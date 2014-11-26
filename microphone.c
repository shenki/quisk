#include <Python.h>
#include <stdlib.h>
#include <math.h>
#include <sys/time.h>
#include <complex.h>
#include <fftw3.h>
#include "quisk.h"
#include <sys/types.h>
#include "microphone.h"
#include "filter.h"

#ifdef MS_WINDOWS
#include <Winsock2.h>
static int mic_cleanup = 0;		// must clean up winsock
#else
#include <sys/socket.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#define		INVALID_SOCKET	-1
#endif

#if DEBUG_IO
static int debug_timer = 1;		// count up number of samples
#endif

// The microphone samples must be 48000 sps or 8000 sps.  The output sample
//  rate is always MIC_OUT_RATE samples per second

// FM needs pre-emphasis and de-emphasis.  See vk1od.net/FM/FM.htm for details.
// For IIR design, see http://www.abvolt.com/research/publications2.htm.

// Microhone preemphasis: boost high frequencies 0.00 to 1.00
double quisk_mic_preemphasis;
// Microphone clipping; try 3.0 or 4.0
double quisk_mic_clip;

// If true, decimate 48000 sps mic samples to 8000 sps for processing
#define DECIM_8000	1

// These are external:
int mic_max_display;				// display value of maximum microphone signal level 0 to 2**15 - 1
int quiskSpotLevel = 0;				// 0 for no spotting; else the level 10 to 1000

static int mic_socket = INVALID_SOCKET;	// send microphone samples to a socket
static double mic_min_level = 0.10;		// Mic levels below this are noise and are ignored

static int mic_level;			// maximum microphone signal level for display
static int mic_timer;			// time to display maximum mic level
static int align4;			// add two bytes to start of audio samples to align to 4 bytes
static double modulation_index = 1.6;	// For FM transmit, the modulation index

static int is_vox = 0;					// Is the VOX level exceeded?
static int vox_level = CLIP16;			// VOX trigger level as a number 0 to CLIP16
static int timeVOX = 2000;				// VOX hang time in milliseconds

#define TX_BLOCK_SHORTS		600		// transmit UDP packet with this many shorts (two bytes) (perhaps + 1)
#define MIC_MAX_HOLD_TIME	400		// Time to hold the maximum mic level on the Status screen in milliseconds

// If TEST_TX_WAV_FILE is defined, then this file is used as the transmit
// audio source.  Otherwise the microphone (if any) is used.  The
// WAV file must be recorded at 48000 Hertz in S16_LE format.
// For example: #define TEST_TX_WAV_FILE "/home/jim/quisk/quisk_test.wav"
//#define TEST_TX_WAV_FILE	"/home/jim/pub/quisk/notdist/quisk.wav"

// If USE_GET_SIN is not zero, replace mic samples with a sin wave at a
// frequency determined by the sidetone slider and an amplitude determined
// by the Spot button level.
// If USE_GET_SIN is 1, pass these samples through the transmit filters.
// If USE_GET_SIN is 2, transmit these samples directly.
#define USE_GET_SIN		0

// If USE_2TONE is not zero, replace samples with a 2-tone test signal.
#define USE_2TONE		0

#ifdef TEST_TX_WAV_FILE
static int wavStart;			// Sound data starts at this offset
static int wavEnd;				// End of sound data
static FILE * wavFp;			// File pointer for WAV file input

static void open_wav(void)
{
	char name[5];
	int size;

	if (!wavFp) {		// Open sound test file
		wavFp = fopen(TEST_TX_WAV_FILE, "rb");
		if (!wavFp) {
			printf("open_wav failed\n");
			return;
		}
		wavEnd = 0;
		while (1) {
			if (fread (name, 4, 1, wavFp) != 1)
				break;
			fread (&size, 4, 1, wavFp);
			name[4] = 0;
			//printf("name %s size %d\n", name, size);
			if (!strncmp(name, "RIFF", 4))
				fseek (wavFp, 4, SEEK_CUR);	// Skip "WAVE"
			else if (!strncmp(name, "data", 4)) {	// sound data starts here
				wavStart = ftell(wavFp);
				wavEnd = wavStart + size;
				break;
			}
			else	// Skip other records
				fseek (wavFp, size, SEEK_CUR);
		}
		//printf("start %d  end %d\n", wavStart, wavEnd);
		if (!wavEnd) {		// Failure to find "data" record
			fclose(wavFp);
			wavFp = NULL;
		}
	}
}

static void get_wav(complex double * buffer, int count)
{
	// Put transmit audio samples from a file into buffer.
	// The sample rate must equal quisk_sound_state.mic_sample_rate.
	int pos, i;
	short sh;

	if (wavFp) {
		pos = ftell (wavFp);
		for (i = 0; i < count; i++) {
			fread(&sh, 2, 1, wavFp);
			buffer[i] = sh * ((double)CLIP32 / CLIP16);
			if (++pos >= wavEnd) {
				fseek (wavFp, wavStart, SEEK_SET);
				pos = wavStart;
			}
		}
	}
}
#endif

#if USE_GET_SIN
static void get_sin(complex double * cSamples, int count)
{	// replace mic samples with a sin wave
	int i;
	double freq;
	complex double phase1;		// Phase increment
	static complex double vector1 = CLIP32 / 2;

	// Use the sidetone slider 0 to 1000 to set frequency
	//freq = (quisk_sidetoneCtrl - 500) / 1000.0 * MIC_OUT_RATE;
	freq = quisk_sidetoneCtrl * 5;
	freq = ((int)freq / 50) * 50;
#if USE_GET_SIN == 2
	phase1 = cexp(I * 2.0 * M_PI * freq / MIC_OUT_RATE);
	count *= MIC_OUT_RATE / quisk_sound_state.mic_sample_rate;
#else
	phase1 = cexp(I * 2.0 * M_PI * freq / quisk_sound_state.mic_sample_rate);
#endif
	for (i = 0; i < count; i++) {
		vector1 *= phase1;
		cSamples[i] = vector1;
	}
#if DEBUG_IO
	if (debug_timer == 0)
		printf ("get_sin freq %.0lf\n", freq);
#endif
}
#endif

#if USE_2TONE
static void get_2tone(complex double * cSamples, int count)
{	// replace mic samples
	int i;
	static complex double phase1=0, phase2;		// Phase increment
	static complex double vector1;
	static complex double vector2;

	if (phase1 == 0) {		// initialize
		phase1 = cexp((I * 2.0 * M_PI * IMD_TONE_1) / quisk_sound_state.mic_sample_rate);
		phase2 = cexp((I * 2.0 * M_PI * IMD_TONE_2) / quisk_sound_state.mic_sample_rate);
		vector1 = CLIP32 / 2.0;
		vector2 = CLIP32 / 2.0;
	}
	for (i = 0; i < count; i++) {
		vector1 *= phase1;
		vector2 *= phase2;
		cSamples[i] = (vector1 + vector2);
	}
}
#endif

static double CcmPeak(double * dsamples, complex double * csamples, int count)
{
	int i, j;
	complex double csample;
	double dtmp, dsample, newlevel, oldlevel;
	static double out_short, out_long;
	static struct Ccmpr {
		int buf_size;
		int index_read;
		double themax;
		double level;
		double * d_samp;
		complex double * c_samp;
		double * levl;
	} dat = {0};

	if ( ! dat.buf_size) {		// initialize; the sample rate is 8000
		dat.buf_size = 8000 * 30 / 1000;	// total delay in samples
		dat.index_read = 0;					// index to output; and then write a new sample here
		dat.themax = 1.0;					// maximum level in the buffer
		dat.level = 1.0;					// current output level
		dat.d_samp = (double *) malloc(dat.buf_size * sizeof(double));		// buffer for double samples
		dat.c_samp = (complex double *) malloc(dat.buf_size * sizeof(complex double));	// buffer for complex samples
		dat.levl = (double *) malloc(dat.buf_size * sizeof(double));		// magnitude of the samples
		for (i = 0; i < dat.buf_size; i++) {
			dat.d_samp[i] = 0;
			dat.c_samp[i] = 0;
			dat.levl[i] = 1.0;
		}
		dtmp = 1.0 / 8000;		// sample time
		out_short  = 1.0 - exp(- dtmp / 0.010);		// short time constant
		out_long   = 1.0 - exp(- dtmp / 3.000);		// long time constant
		return 1.0;
	}
	for (i = 0; i < count; i++) {
		if (dsamples) {
			dsample = dsamples[i];
			dsamples[i] = dat.d_samp[dat.index_read] / dat.level;	// FIFO output
			dat.d_samp[dat.index_read] = dsample;		// write new sample at read index
			newlevel = fabs(dsample);
		}
		else {
			csample = csamples[i];
			csamples[i] = dat.c_samp[dat.index_read] / dat.level;	// FIFO output
			dat.c_samp[dat.index_read] = csample;		// write new sample at read index
			newlevel = cabs(csample);
		}
		oldlevel = dat.levl[dat.index_read];
		dat.levl[dat.index_read] = newlevel;
		if (newlevel < dat.themax && oldlevel < dat.themax) {		// some other sample is the maximum
			// no change to dat.themax
		}
		else if (newlevel > dat.themax && newlevel > oldlevel) {	// newlevel is the maximum
			dat.themax = newlevel;
		}
		else {		// search for the maximum level
			dat.themax = 0;		// Find the maximim level in the buffer
			for (j = 0; j < dat.buf_size; j++) {
				if (dat.levl[j] > dat.themax)
					dat.themax = dat.levl[j];
			}
		}
// Increase dat.level if the maximum level is greater than 1.0;
// decrease it slowly back to 1.0 if it is lower.  Output is modulated by dat.level.
		if (dat.themax > 1.0)	// increase rapidly to the peak level
			dat.level = dat.level * (1.0 - out_short) + dat.themax * out_short;
		else				// decrease slowly back to 1.0
			dat.level = dat.level * (1.0 - out_long) + 1.0 * out_long;
		if (++dat.index_read >= dat.buf_size)
			dat.index_read = 0;
	}
	return dat.level;
}

static int tx_filter(complex double * filtered, int count)
{	// Input samples are creal(filtered), output is filtered.  The input rate must be 8000 or 48000 sps.
	int i, is_ssb;
	int sample_rate = 8000;
	double dsample, dtmp, magn;
	complex double csample;
	static double inMax=0.3;
	static double x_1=0;
	static double aaa, bbb, ccc, Xmin, Xmax, Ymax;
	static int samples_size = 0;
	static double * dsamples = NULL;
	static complex double * csamples = NULL;
	static double time_long, time_short;
	static struct quisk_dFilter filtDecim, dfiltInterp;
	static struct quisk_dFilter filtAudio1, filtAudio2, dfiltAudio3;
	static struct quisk_cFilter cfiltAudio3, cfiltInterp;
	static struct quisk_dFilter filter1={NULL}, filter2;
#if DEBUG_IO
	char * clip;
	static double dbOut = 0, Level0 = 0, Level1 = 0, Level2 = 0, Level3 = 0, Level4 = 0;
#endif
	is_ssb = (rxMode == 2 || rxMode == 3);
	if (!filtered) {		// initialization
		if (! filter1.dCoefs) {
			quisk_filt_dInit(&filter1, quiskMicFilt8Coefs, sizeof(quiskMicFilt8Coefs)/sizeof(double));
			quisk_filt_dInit(&filter2, quiskMicFilt8Coefs, sizeof(quiskMicFilt8Coefs)/sizeof(double));
			quisk_filt_dInit(&filtDecim,  quiskLpFilt48Coefs, sizeof(quiskLpFilt48Coefs)/sizeof(double));
			quisk_filt_dInit(&dfiltInterp, quiskLpFilt48Coefs, sizeof(quiskLpFilt48Coefs)/sizeof(double));
			quisk_filt_cInit(&cfiltInterp, quiskLpFilt48Coefs, sizeof(quiskLpFilt48Coefs)/sizeof(double));
			quisk_filt_dInit(&filtAudio1, quiskFiltTx8kAudioB, sizeof(quiskFiltTx8kAudioB)/sizeof(double));
			quisk_filt_dInit(&filtAudio2, quiskFiltTx8kAudioB, sizeof(quiskFiltTx8kAudioB)/sizeof(double));
			quisk_filt_dInit(&dfiltAudio3, quiskFiltTx8kAudioB, sizeof(quiskFiltTx8kAudioB)/sizeof(double));
			quisk_filt_cInit(&cfiltAudio3, quiskFiltTx8kAudioB, sizeof(quiskFiltTx8kAudioB)/sizeof(double));
			dtmp = 1.0 / sample_rate;		// sample time
			time_long   = 1.0 - exp(- dtmp / 3.000);
			time_short  = 1.0 - exp(- dtmp / 0.005);
			Ymax = pow(10.0,  - 1 / 20.0);				// maximum y
			Xmax = pow(10.0,  3 / 20.0);				// x where slope is zero; for x > Xmax, y == Ymax
			Xmin = Ymax - fabs(Ymax - Xmax);		// x where slope is 1 and y = x; start of compression
			aaa = 1.0 / (2.0 * (Xmin - Xmax));		// quadratic
			bbb = -2.0 * aaa * Xmax;
			ccc = Ymax - aaa * Xmax * Xmax - bbb * Xmax;
#if DEBUG_IO
			printf("Compress to %.2lf dB from %.2lf to %.2lf dB\n",
				20 * log10(Ymax), 20 * log10(Xmin), 20 * log10(Xmax));
#endif
		}
		if (is_ssb) {
		  quisk_filt_tune(&filter1, 1650.0 / sample_rate, rxMode != 2);
		  quisk_filt_tune(&filter2, 1650.0 / sample_rate, rxMode != 2);
		}
		return 0;
	}
	// check size of dsamples[] and csamples[] buffer
    if (count > samples_size) {
        samples_size = count * 2;
        if (dsamples)
            free(dsamples);
        if (csamples)
            free(csamples);
        dsamples = (double *)malloc(samples_size * sizeof(double));
        csamples = (complex double *)malloc(samples_size * sizeof(complex double));
    }
	// copy to dsamples[], normalize to +/- 1.0
	for (i = 0; i < count; i++)
		dsamples[i] = creal(filtered[i]) / CLIP16;
	// Decimate to 8000 Hz
	if (quisk_sound_state.mic_sample_rate != sample_rate)
		count = quisk_dDecimate(dsamples, count, &filtDecim, quisk_sound_state.mic_sample_rate / sample_rate);
	// restrict bandwidth 300 to 2700 Hz
	count = quisk_dFilter(dsamples, count, &filtAudio1);
#if DEBUG_IO
	// Measure peak input audio level
	for (i = 0; i < count; i++) {
		magn = fabs(dsamples[i]);
		if (magn > Level0)
			Level0 = magn;
	}
#endif
	// high pass filter for preemphasis: See Radcom, January 2010, page 76.
	// quisk_mic_preemphasis == 1 was measured as 6 dB / octave.
	// gain at 800 Hz was measured as 0.104672.
	for (i = 0; i < count; i++) {
		dtmp = dsamples[i];
		dsamples[i] = dtmp - quisk_mic_preemphasis * x_1;
		x_1 = dtmp;	// delayed sample
		dsamples[i] *= 2;		// compensate for loss
#if DEBUG_IO
		magn = fabs(dsamples[i]);
		if (magn > Level1)
			Level1 = magn;
#endif
	}
	if (is_ssb) {	// SSB
		// FIR bandpass filter; separate into I and Q
		for (i = 0; i < count; i++) {
			csample = quisk_dC_out(dsamples[i], &filter1);
			// Measure average peak input audio level and normalize
			magn = cabs(csample);
			if (magn > inMax)
				inMax = inMax * (1 - time_short) + time_short * magn;
			else if(magn > mic_min_level)
				inMax = inMax * (1 - time_long) + time_long * magn;
			else
				inMax = inMax * (1 - time_long) + time_long * mic_min_level;
			csample /= inMax;
			magn /= inMax;
#if DEBUG_IO
			if (magn > Level2)
				Level2 = magn;
#endif
			// Audio compression.
			csample *= quisk_mic_clip;
			magn *= quisk_mic_clip;
			if (magn > 1.0)
				csample = csample / magn;
			dsamples[i] = creal(csample);
		}
	}
	else {		// AM and FM
		// Measure average peak input audio level and normalize
		for (i = 0; i < count; i++) {
			dsample = dsamples[i];
			magn = fabs(dsample);
			if (magn > inMax)
				inMax = inMax * (1 - time_short) + time_short * magn;
			else if(magn > mic_min_level)
				inMax = inMax * (1 - time_long) + time_long * magn;
			else
				inMax = inMax * (1 - time_long) + time_long * mic_min_level;
			dsample /= inMax;
			magn /= inMax;
#if DEBUG_IO
			if (magn > Level2)
				Level2 = magn;
#endif
		// Audio compression.
			dsample *= quisk_mic_clip;
			magn *= quisk_mic_clip;
			if (magn < Xmin)
				dsamples[i] = dsample;
			else if (magn > Xmax)
				dsamples[i] = copysign(Ymax, dsample);
			else
				dsamples[i] = copysign(aaa * magn * magn + bbb * magn + ccc, dsample);
		}
	}
	// remove clipping distortion; restrict bandwidth 300 to 2700 Hz
	count = quisk_dFilter(dsamples, count, &filtAudio2);
	if (is_ssb) {	// SSB
		// FIR bandpass filter; separate into I and Q
		for (i = 0; i < count; i++) {
			csamples[i] = quisk_dC_out(creal(dsamples[i]), &filter2);
#if DEBUG_IO
			magn = cabs(csamples[i]);
			if (magn > Level3)
				Level3 = magn;
#endif
		}
		// round off peaks
		CcmPeak(NULL, csamples, count);
#if DEBUG_IO
		for (i = 0; i < count; i++) {
			magn = cabs(csamples[i]);
			if (magn > Level4)
				Level4 = magn;
		}
#endif
		// remove clipping distortion
		count = quisk_cDecimate(csamples, count, &cfiltAudio3, 1);
		// Interpolate up to 48000
		if (MIC_OUT_RATE != sample_rate)
			count = quisk_cInterpolate(csamples, count, &cfiltInterp, MIC_OUT_RATE / sample_rate);
		// convert back to 16 bits and reduce level to allow headroom
		for (i = 0; i < count; i++) {
			csamples[i] /= 1.3;
			magn = cabs(csamples[i]);
			if (magn > 1.0)
				filtered[i] = csamples[i] / magn * CLIP16;
			else
				filtered[i] = csamples[i] * CLIP16;
#if DEBUG_IO
			if (magn > dbOut)
				dbOut = magn;
#endif
		}
	}
	else {		// AM and FM
#if DEBUG_IO
		for (i = 0; i < count; i++) {
			magn = fabs(dsamples[i]);
			if (magn > Level3)
				Level3 = magn;
		}
#endif
		// round off peaks
		CcmPeak(dsamples, NULL, count);
#if DEBUG_IO
		for (i = 0; i < count; i++) {
			magn = fabs(dsamples[i]);
			if (magn > Level4)
				Level4 = magn;
		}
#endif
		// remove clipping distortion
		count = quisk_dFilter(dsamples, count, &dfiltAudio3);
		// Interpolate up to 48000
		if (MIC_OUT_RATE != sample_rate)
			count = quisk_dInterpolate(dsamples, count, &dfiltInterp, MIC_OUT_RATE / sample_rate);
		// convert back to 16 bits and reduce level to allow headroom
		for (i = 0; i < count; i++) {
			dsamples[i] /= 1.3;
			magn = fabs(dsamples[i]);
#if DEBUG_IO
			if (magn > dbOut)
				dbOut = magn;
#endif
			if (magn > 1.0)
				filtered[i] = dsamples[i] / magn * CLIP16;
			else
				filtered[i] = dsamples[i] * CLIP16;
		}
	}
#if DEBUG_IO
	if (debug_timer == 0) {
		if (dbOut > 1.0)
			clip = "Clip";
		else
			clip = "";
		dbOut = 20 * log10(dbOut);
		printf ("pre %3.1lf dB clip %2.0lf InMax %6.2lf   Level0 %6.2lf  Level1 %6.2lf  Level2 %6.2lf  Level3 %6.2lf  Level4 %6.2lf  dbOut %6.2lf  %s\n",
			quisk_mic_preemphasis, 20 * log10(quisk_mic_clip), 20 * log10(inMax), 20 * log10(Level0),
			20 * log10(Level1), 20 * log10(Level2), 20 * log10(Level3),  20 * log10(Level4), dbOut, clip);
		Level0 = Level1 = Level2 = Level3 = Level4 = dbOut = 0;
	}
	//QuiskPrintTime("    tx_filter", 2);
#endif
	return count;
}

static int tx_filter_digital(complex double * filtered, int count, double volume)
{	// Input samples are creal(filtered), output is filtered.
	// This filter has minimal processing and is used for digital modes.
	int i;
	double dsample, amplitude;
	complex double csample;

	static struct quisk_dFilter filter1;
#if DEBUG_IO
	double x;
	static double peakIn = 0, peakOut2 = 0;		// input/output level
#endif
	if (!filtered) {		// initialization
		quisk_filt_dInit(&filter1, quiskMic5Filt48Coefs, sizeof(quiskMic5Filt48Coefs)/sizeof(double));
		quisk_filt_tune(&filter1, 2650.0 / 48000, rxMode != 2);
		return 0;
	}
#if DEBUG_IO
	//QuiskPrintTime("", -2);
#endif
	for (i = 0; i < count; i++) {
		dsample = creal(filtered[i]) / CLIP16;		// normalize to +/- 1.0
#if DEBUG_IO
		x = fabs(dsample);
		if (x > peakIn)
			peakIn = x;
#endif
		// FIR bandpass filter; separate into I and Q
		csample = quisk_dC_out(dsample, &filter1);
		amplitude = cabs(csample);
#if DEBUG_IO
		if (amplitude > peakOut2)
			peakOut2 = amplitude;
#endif
		if (amplitude > 1.0)
			csample /= amplitude;
		filtered[i] = csample * CLIP16 * volume;		// convert back to 16 bits
	}

//printf("%5.2lf\n", increasing);
#if DEBUG_IO
	if (debug_timer == 0) {
		printf ("peakIn %10.6lf  peakOut2 %10.6lf", peakIn, peakOut2);
		if (peakOut2 > 1.0)
			printf ("  CLIP\n");
		else
			printf ("\n");
		peakIn = peakOut2 = 0;
	}
	//QuiskPrintTime("    tx_filter", 2);
#endif
	return count;
}

PyObject * quisk_get_tx_filter(PyObject * self, PyObject * args)
{  // return the TX filter response to display on the graph
// This is for debugging.  Change quisk.py to call QS.get_tx_filter() instead
// of QS.get_filter().
	int i, j, k;
	int freq, time;
	PyObject * tuple2;
	complex double cx;
	double scale;
	double * average, * fft_window, * bufI, * bufQ;
	fftw_complex * samples, * pt;		// complex data for fft
	fftw_plan plan;						// fft plan
	double phase, delta;
	int nTaps = 325;

	if (!PyArg_ParseTuple (args, ""))
		return NULL;

	// Create space for the fft of size data_width
	pt = samples = (fftw_complex *) fftw_malloc(sizeof(fftw_complex) * data_width);
	plan = fftw_plan_dft_1d(data_width, pt, pt, FFTW_FORWARD, FFTW_MEASURE);
	average = (double *) malloc(sizeof(double) * (data_width + nTaps));
	fft_window = (double *) malloc(sizeof(double) * data_width);
	bufI = (double *) malloc(sizeof(double) * nTaps);
	bufQ = (double *) malloc(sizeof(double) * nTaps);

	for (i = 0, j = -data_width / 2; i < data_width; i++, j++)	// Hanning
		fft_window[i] = 0.5 + 0.5 * cos(2. * M_PI * j / data_width);

	for (i = 0; i < data_width + nTaps; i++)
		average[i] = 0.5;	// Value for freq == 0
	for (freq = 1; freq < data_width / 2.0 - 10.0; freq++) {
	//freq = data_width * 0.2 / 48.0;
		delta = 2 * M_PI / data_width * freq;
		phase = 0;
		// generate some initial samples to fill the filter pipeline
		for (time = 0; time < data_width + nTaps; time++) {
			average[time] += cos(phase);	// current sample
			phase += delta;
			if (phase > 2 * M_PI)
				phase -= 2 * M_PI;
		}
	}
	// now filter the signal using the transmit filter
	tx_filter(NULL, 0);				// initialize
	scale = 1.0;
	for (i = 0; i < data_width; i++)
		if (fabs(average[i + nTaps]) > scale)
			scale = fabs(average[i + nTaps]);
	scale = CLIP16 / scale;		// limit to CLIP16
	for (i = 0; i < nTaps; i++)
		samples[i] = average[i] * scale;
	tx_filter(samples, nTaps);			// process initial samples
	for (i = 0; i < data_width; i++)
		samples[i] = average[i + nTaps] * scale;
	tx_filter(samples, data_width);	// process the samples

	for (i = 0; i < data_width; i++)	// multiply by window
		samples[i] *= fft_window[i];
	fftw_execute(plan);		// Calculate FFT
	// Normalize and convert to log10
	scale = 0.3 / data_width / scale;
	for (k = 0; k < data_width; k++) {
		cx = samples[k];
		average[k] = cabs(cx) * scale;
		if (average[k] <= 1e-7)		// limit to -140 dB
			average[k] = -7;
		else
			average[k] = log10(average[k]);
	}
	// Return the graph data
	tuple2 = PyTuple_New(data_width);
	i = 0;
	// Negative frequencies:
	for (k = data_width / 2; k < data_width; k++, i++)
		PyTuple_SetItem(tuple2, i, PyFloat_FromDouble(20.0 * average[k]));

	// Positive frequencies:
	for (k = 0; k < data_width / 2; k++, i++)
		PyTuple_SetItem(tuple2, i, PyFloat_FromDouble(20.0 * average[k]));

	free(bufQ);
	free(bufI);
	free(average);
	free(fft_window);
	fftw_destroy_plan(plan);
	fftw_free(samples);

	return tuple2;
}

// udp_iq has an initial zero followed by the I/Q samples.
// The initial zero is sent iff align4 == 1.

static void transmit_udp(complex double * cSamples, int count)
{	// Send count samples.  Each sample is sent as two shorts (4 bytes) of I/Q data.
	// Transmission is delayed until a whole block of data is available.
	int i, sent;
	static short udp_iq[TX_BLOCK_SHORTS + 1] = {0};
	static int udp_size = 1;

	if (mic_socket == INVALID_SOCKET)
		return;
	if ( ! cSamples) {		// initialization
		udp_size = 1;
		udp_iq[0] = 0;	// should not be necessary
		return;
	}
	for (i = 0; i < count; i++) {	// transmit samples
		udp_iq[udp_size++] = (short)creal(cSamples[i]);
		udp_iq[udp_size++] = (short)cimag(cSamples[i]);
		if (udp_size >= TX_BLOCK_SHORTS) {	// check count
			if (align4)
				sent = send(mic_socket, (char *)udp_iq, udp_size * 2, 0);
			else
				sent = send(mic_socket, (char *)udp_iq + 1, --udp_size * 2, 0);
			if (sent != udp_size * 2)
				printf("Send socket returned %d\n", sent);
			udp_size = 1;
		}
	}
}

static void transmit_mic_carrier(complex double * cSamples, int count, double level)
{	// send a CW carrier instead of mic samples
	int i;

	for (i = 0; i < count; i++)		// transmit a carrier equal to the number of samples
		cSamples[i] = level * CLIP16;
	transmit_udp(cSamples, count);
}

static void transmit_mic_imd(complex double * cSamples, int count, double level)
{	// send a 2-tone test signal instead of mic samples
	int i;
	complex double v;
	static complex double phase1=0, phase2;		// Phase increment
	static complex double vector1;
	static complex double vector2;

	if (phase1 == 0) {		// initialize
		phase1 = cexp((I * 2.0 * M_PI * IMD_TONE_1) / MIC_OUT_RATE);
		phase2 = cexp((I * 2.0 * M_PI * IMD_TONE_2) / MIC_OUT_RATE);
		vector1 = CLIP16 / 2.0;
		vector2 = CLIP16 / 2.0;
	}
	for (i = 0; i < count; i++) {	// transmit a carrier equal to the number of samples
		vector1 *= phase1;
		vector2 *= phase2;
		v = level * (vector1 + vector2);
		cSamples[i] = v;
	}
	transmit_udp(cSamples, count);
}

int quisk_process_microphone(int mic_sample_rate, complex double * cSamples, int count)
{
	int i, sample, maximum, interp, mic_interp;
	double d;
	static struct quisk_cFilter filtInterp={NULL};

// Microphone sample are input at mic_sample_rate.  But after processing,
// the output rate is MIC_OUT_RATE.
	interp = MIC_OUT_RATE / mic_sample_rate;

#if 0
	// Measure soundcard actual sample rate
	static time_t seconds = 0;
	static int total = 0;
	struct timeval tb;
	static double dtime;

	gettimeofday(&tb);
	total += count;
	if (seconds == 0) {
		seconds = tb.tv_sec;
		dtime = tb.tv_sec + 0.000001 * tb.tv_usec;
	}		
	else if (tb.tv_sec - seconds > 4) {
		printf("Mic soundcard rate %.3f\n", total / (tb.tv_sec + .000001 * tb.tv_usec - dtime));
		seconds = tb.tv_sec;
		printf("backlog %d, count %d\n", backlog, count);
	}
#endif

#if DEBUG_IO
	//QuiskPrintTime("", -1);
#endif

#if DEBUG_IO
	debug_timer += count;
	if (debug_timer >= mic_sample_rate)		// one second
		debug_timer = 0;
#endif
	
#ifdef TEST_TX_WAV_FILE
	get_wav(cSamples, count);	// Replace mic samples with sound from a WAV file
#endif
#if USE_GET_SIN
	get_sin(cSamples, count);	// Replace mic samples with a sin wave
#endif
#if USE_2TONE
	get_2tone(cSamples, count);	// Replace mic samples with a 2-tone test signal
#endif
	maximum = 1;
	for (i = 0; i < count; i++) {	// measure maximum microphone level for display
		cSamples[i] *= (double)CLIP16 / CLIP32;	// convert 32-bit samples to 16 bits
		d = creal(cSamples[i]);
		sample = (int)fabs(d);
		if (sample > maximum)
			maximum = sample;
	}
    if (maximum > vox_level) {
		is_vox = mic_sample_rate / 1000 * timeVOX;		// reset timer to maximum
	}
	else if(is_vox) {
		is_vox -= count;		// decrement timer
		if (is_vox < 0)
			is_vox = 0;
	}
	if (maximum > mic_level)
		mic_level = maximum;
	mic_timer -= count;		// time out the max microphone level to display
	if (mic_timer <= 0) {
		mic_timer = mic_sample_rate / 1000 * MIC_MAX_HOLD_TIME;
		mic_max_display = mic_level;
		mic_level = 1;
	}

	if (quisk_is_key_down()) {
#if USE_GET_SIN == 2
		transmit_udp(cSamples, count * interp);
#else
		switch (rxMode) {
		case 2:		// LSB
		case 3:		// USB
			if (quisk_record_state == PLAYBACK) {
				count = tx_filter_digital(cSamples, count, 0.9);	// filter samples, minimal processing
				transmit_udp(cSamples, count);
			}
			else if (quiskSpotLevel == 0) {
				count = tx_filter(cSamples, count);		// filter samples
				transmit_udp(cSamples, count);
			}
			else {
				count *= interp;
				transmit_mic_carrier(cSamples, count, quiskSpotLevel / 1000.0);
			}
			break;
		case 4:		// AM
			if (quisk_record_state != PLAYBACK)		// no audio processing for recorded sound
				count = tx_filter(cSamples, count);
			for (i = 0; i < count; i++)	// transmit (0.5 + ampl/2, 0)
				cSamples[i] = (creal(cSamples[i]) + CLIP16) * 0.5;
			transmit_udp(cSamples, count);
			break;
		case 5:		// FM
			if (quisk_record_state != PLAYBACK)		// no audio processing for recorded sound
				count = tx_filter(cSamples, count);
			for (i = 0; i < count; i++) {	// this is phase modulation == FM and 6 dB /octave preemphasis
				cSamples[i] = CLIP16 * cexp(I * creal(cSamples[i]) / CLIP16 * modulation_index);
			}
  			transmit_udp(cSamples, count);
  			break;
		case 7:		// digital modes
		case 8:
		case 9:
			if (quiskSpotLevel == 0) {
				count = tx_filter_digital(cSamples, count, 1.0);	// filter samples, minimal processing
				transmit_udp(cSamples, count);
			}
			else {
				count *= interp;
				transmit_mic_carrier(cSamples, count, quiskSpotLevel / 1000.0);
			}
  			break;
		case 10:	// transmit IMD 2-tone test
			count *= interp;
			transmit_mic_imd(cSamples, count, 1.0);
			break;
		case 11:
			count *= interp;
			transmit_mic_imd(cSamples, count, 1.0 / sqrt(2.0));
			break;
		case 12:
			count *= interp;
			transmit_mic_imd(cSamples, count, 0.5);
			break;
		}
#endif
	}
	else if (quisk_record_state == RECORD_MIC) {
		switch (rxMode) {
		case 2:		// LSB
		case 3:		// USB
			count = tx_filter(cSamples, count);	// filter samples
			break;
		case 4:		// AM
			count = tx_filter(cSamples, count);
			break;
		case 5:		// FM
			count = tx_filter(cSamples, count);
  			break;
		default:
			for (i = 0; i < count; i++)
				cSamples[i] = 0;
			break;
		}
		// Perhaps interpolate the mic samples back to the sound play rate
		mic_interp = quisk_sound_state.playback_rate / MIC_OUT_RATE;
		if (mic_interp > 1) {
			if (! filtInterp.dCoefs)
				quisk_filt_cInit(&filtInterp, quiskFilt12_19Coefs, sizeof(quiskFilt12_19Coefs)/sizeof(double));
			count = quisk_cInterpolate(cSamples, count, &filtInterp, mic_interp);
		}
		quisk_tmp_record(cSamples, count, (double)CLIP32 / CLIP16);	// convert 16 to 32 bits
	}
	else if (DEBUG_MIC) {
		count = tx_filter(cSamples, count);
	}

#if DEBUG_IO
	//QuiskPrintTime("    process_mic", 1);
#endif
	return count;
}

PyObject * quisk_set_tx_audio(PyObject * self, PyObject * args, PyObject * keywds)
{  /* Call with keyword arguments ONLY; change Tx audio parameters */
	static char * kwlist[] = {"vox_level", "vox_time", "mic_clip", "mic_preemphasis", NULL} ;
	int vlevel = -9999, clevel = -9999;

	if (!PyArg_ParseTupleAndKeywords (args, keywds, "|iiid", kwlist,
			&vlevel, &timeVOX, &clevel, &quisk_mic_preemphasis))
		return NULL;
	if (vlevel != -9999)
		vox_level = (int)(pow(10.0, vlevel / 20.0) * CLIP16);	// Convert dB to 16-bit sample
	if (clevel != -9999)
		quisk_mic_clip = pow(10.0, clevel / 20.0);	// Convert dB to factor
	Py_INCREF (Py_None);
	return Py_None;
}

PyObject * quisk_is_vox(PyObject * self, PyObject * args)
{	/* return the VOX state */
	if (!PyArg_ParseTuple (args, ""))
		return NULL;
	return PyInt_FromLong(is_vox);
}

void quisk_close_mic(void)
{
	if (mic_socket != INVALID_SOCKET) {
		close(mic_socket);
		mic_socket = INVALID_SOCKET;
	}
#ifdef MS_WINDOWS
	if (mic_cleanup)
		WSACleanup();
#endif
}

void quisk_open_mic(void)
{
	struct sockaddr_in Addr;
	int sndsize = 48000;
#if DEBUG_IO
	int intbuf;
#ifdef MS_WINDOWS
	int bufsize = sizeof(int);
#else
	socklen_t bufsize = sizeof(int);
#endif
#endif

#ifdef MS_WINDOWS
	WORD wVersionRequested;
	WSADATA wsaData;
#endif

	modulation_index = QuiskGetConfigDouble("modulation_index", 1.6);
	if (quisk_sound_state.tx_audio_port == 0x553B)
		align4 = 0;		// Using old port: data starts at byte 42.
	else
		align4 = 1;		// Start data at byte 44; align to dword
	if (quisk_sound_state.mic_ip[0]) {
#ifdef MS_WINDOWS
		wVersionRequested = MAKEWORD(2, 2);
		if (WSAStartup(wVersionRequested, &wsaData) != 0)
			return;		// failure to start winsock
		mic_cleanup = 1;
#endif
		mic_socket = socket(PF_INET, SOCK_DGRAM, 0);
		if (mic_socket != INVALID_SOCKET) {
			setsockopt(mic_socket, SOL_SOCKET, SO_SNDBUF, (char *)&sndsize, sizeof(sndsize));
			Addr.sin_family = AF_INET;
// This is the UDP port for TX microphone samples, and must agree with the microcontroller.
			Addr.sin_port = htons(quisk_sound_state.tx_audio_port);
#ifdef MS_WINDOWS
			Addr.sin_addr.S_un.S_addr = inet_addr(quisk_sound_state.mic_ip);
#else
			inet_aton(quisk_sound_state.mic_ip, &Addr.sin_addr);
#endif
			if (connect(mic_socket, (const struct sockaddr *)&Addr, sizeof(Addr)) != 0) {
				close(mic_socket);
				mic_socket = INVALID_SOCKET;
			}
			else {
#if DEBUG_IO
				if (getsockopt(mic_socket, SOL_SOCKET, SO_SNDBUF, (char *)&intbuf, &bufsize) == 0)
					printf("UDP mic socket send buffer size %d\n", intbuf);
				else
					printf ("Failure SO_SNDBUF\n");
#endif
			}
		}
	}
}

void quisk_set_tx_mode(void)	// called when the mode rxMode is changed
{
	tx_filter(NULL, 0);
	tx_filter_digital(NULL, 0, 0.0);
	transmit_udp(NULL, 0);
#ifdef TEST_TX_WAV_FILE
	if (!wavFp)			// convenient place to open file
		open_wav();
#endif
}
