#include <Python.h>
#include <stdlib.h>
#include <math.h>
#include <complex.h>	// Use native C99 complex type for fftw3
#include <fftw3.h>
#include <sys/types.h>

#ifdef MS_WINDOWS
#include <Winsock2.h>
#define QUISK_SHUT_RD	SD_RECEIVE
#define QUISK_SHUT_BOTH	SD_BOTH
static int cleanupWSA = 0;			// Must we call WSACleanup() ?
#else
#include <sys/socket.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#define INVALID_SOCKET	-1
#define QUISK_SHUT_RD	SHUT_RD
#define QUISK_SHUT_BOTH	SHUT_RDWR
#endif

#include "quisk.h"
#include "filter.h"

#define DEBUG		0

#define FM_FILTER_DEMPH		300.0				// Frequency of FM lowpass de-emphasis filter
#define AGC_DELAY			15					// Delay in AGC buffer in milliseconds
#define AGC_MAX_OUT			(CLIP32 * 0.7)		// Maximum AGC output level
#define FFT_ARRAY_SIZE		4					// Number of FFTs

static int fft_error;			// fft error count
typedef struct fftd {
	fftw_complex * samples;		// complex data for fft
	fftw_plan plan;				// fft plan for fftW
	int index;					// position of next fft sample
	int filled;					// whether the fft is ready to run
	int block;					// block number 0, 1, ...
} fft_data;

struct AgcState {		// Store state information for the AGC
	int buf_size;
	int index_read;
	int index_start;
	int is_clipping;
	double themax;
	double gain;
	double delta;
	double target_gain;
	complex double * c_samp;
};

static fft_data fft_data_array[FFT_ARRAY_SIZE];		// Data for several FFTs
static int fft_data_index = 0;						// Write the current samples to this FFT

static double * fft_window;		// Window for FFT data
static double * current_graph;	// current graph data as returned
static int use_remove_dc=1;		// Remove DC from samples

static PyObject * QuiskError;		// Exception for this module
static PyObject * pyApp;		// Application instance
static int fft_size;			// size of fft, e.g. 1024
int data_width;				// number of points to return as graph data; fft_size * n
int rxMode;				// 0 to 9, 10 to 12: CWL, CWU, LSB, USB, AM, FM, EXT, DGT-U, DGT-L, DGT-IQ, IMD(10 to 12)
int quisk_noise_blanker;		// noise blanker level, 0 for off
static int quisk_auto_notch;	// auto notch control
PyObject * quisk_pyConfig=NULL;	// Configuration module instance
long quisk_mainwin_handle;		// Handle of the main window
static int graphX;			// Origin of first X value for graph data
static int graphY;			// Origin of 0 dB for graph data
static int average_count;		// Number of FFT's to average for graph
static double graphScale;		// Scale factor for graph
static complex double testtonePhase;        // Phase increment for test tone
double quisk_audioVolume;		// Audio output level, 0.0 to 1.0
static double cFilterI[MAX_FILTER_SIZE];	// Digital filter coefficients for receive
static double cFilterQ[MAX_FILTER_SIZE];	// Digital filter coefficients
static int sizeFilter;			// Number of coefficients for filters
static int isFDX;			// Are we in full duplex mode?
static int filter_bandwidth;		// Current filter bandwidth in Hertz
static int quisk_decim_srate;				// Sample rate after decimation
static int quisk_demod_srate;				// Sample rate after demodulation
static int quisk_filter_srate=48000;		// Frequency for filters
static int split_rxtx;						// Are we in split rx/tx mode?
static int kill_audio;					// Replace radio sound with silence
static int quisk_transmit_mode;			// Are we in transmit mode for semi-break-in CW?
static int fft_sample_rate;				// Sample rate on the graph (not the audio channel) -fft_srate/2 < freq < +fft_srate/2
static int scan_blocks=0;			// Number of FFT blocks for scan; or zero
static int scan_sample_rate=1;      // Sample rate to use while scanning
static double scan_valid=0.84;		// Fraction of each FFT block that is valid
static int scan_vfo0;
static int scan_deltaf;
static int graph_refresh;				// Graph refresh rate from the config file
static int multiple_sample_rates=0;     // Hardware supports multiple different sample rates
static int vfo_screen;                  // The VFO (center) frequency on the FFT screen
static int vfo_audio;                   // VFO frequency for the audio channel

static double sidetoneVolume;		// Audio output level of the CW sidetone, 0.0 to 1.0
static int keyupDelay;			// Play silence after sidetone ends
static complex double sidetonePhase;		// Phase increment for sidetone
int quisk_sidetoneCtrl;			// sidetone control value 0 to 1000

static double agcReleaseGain=80;		// AGC maximum gain
static double agcTimeRelease=0.01;		// Release time fraction (not seconds)
static double squelch_level;			// setting of squelch control
static int quisk_invert_spectrum = 0;	// Invert the input RF spectrum

static double Smeter;			// Measured RMS signal strength
static int rx_tune_freq;		// Receive tuning frequency as +/- sample_rate / 2
int quisk_tx_tune_freq;			// Transmit tuning frequency as +/- sample_rate / 2
static int rit_freq;			// RIT frequency in Hertz

#define RX_UDP_SIZE		1442		// Expected size of UDP samples packet
static int rx_udp_socket = INVALID_SOCKET;		// Socket for receiving ADC samples from UDP
static int rx_udp_started = 0;		// Have we received any data yet?
int quisk_using_udp = 0;			// Are we using rx_udp_socket?  No longer used, but provided for backward compatibility.
static double rx_udp_gain_correct = 0;		// Small correction for different decimation rates
static double rx_udp_clock;			// Clock frequency for UDP samples
static int use_rx_udp;              // from the config file

static int is_little_endian;		// Test byte order; is it little-endian?

enum quisk_rec_state quisk_record_state = IDLE;
static float * quisk_record_buffer;
static int quisk_record_bufsize;
static int quisk_record_index;
static int quisk_play_index;
static int quisk_mic_index;
static int quisk_record_full;

// These are used to measure the frequency of a continuous RF signal.
static void measure_freq(complex double *, int, int);
static double measured_frequency;
static int measure_freq_mode=0;

//These are used to measure the demodulated audio voltage
static double measured_audio;
static double measure_audio_sum;
static int measure_audio_count;
static int measure_audio_time=1;

#if 0
static int fFracDecim(double * dSamples, int nSamples, double fdecim)
{
	int i, nout;
	double xm0, xm1, xm2, xm3;
	static double dindex = 1;
	static double y0=0, y1=0, y2=0, y3=0;
	static int in=0, out=0;

	in += nSamples;
	nout = 0;
	for (i = 0; i < nSamples; i++) {
		y3 = dSamples[i];
		if (dindex < 1 || dindex >= 2.4)
			printf ("dindex %.5f  fdecim %.8f\n", dindex, fdecim);
		if (dindex < 2) {
#if 0
				dSamples[nout++] = (1 - (dindex - 1)) * y1 + (dindex - 1) * y2;
#else
				xm0 = dindex - 0;
				xm1 = dindex - 1;
				xm2 = dindex - 2;
				xm3 = dindex - 3;
				dSamples[nout++] = xm1 * xm2 * xm3 * y0 / -6.0 + xm0 * xm2 * xm3 * y1 / 2.0 +
					xm0 * xm1 * xm3 * y2 / -2.0 + xm0 * xm1 * xm2 * y3 / 6.0;
#endif
			out++;
			dindex += fdecim - 1;
			y0 = y1;
			y1 = y2;
			y2 = y3;
		}
		else {
			if (dindex > 2.5) printf ("Skip at %.2f\n", dindex);
			y0 = y1;
			y1 = y2;
			y2 = y3;
			dindex -= 1;
		}
	}
	//printf ("in %d out %d\n", in, out);
	return nout;
}
#endif

static int cFracDecim(complex double * cSamples, int nSamples, double fdecim)
{
// Fractional decimation of I/Q signals works poorly because it introduces aliases and birdies.
	int i, nout;
	double xm0, xm1, xm2, xm3;
	static double dindex = 1;
	static complex double c0=0, c1=0, c2=0, c3=0;
	static int in=0, out=0;

	in += nSamples;
	nout = 0;
	for (i = 0; i < nSamples; i++) {
		c3 = cSamples[i];
		if (dindex < 1 || dindex >= 2.4)
			printf ("dindex %.5f  fdecim %.8f\n", dindex, fdecim);
		if (dindex < 2) {
#if 0
				cSamples[nout++] = (1 - (dindex - 1)) * c1 + (dindex - 1) * c2;
#else
				xm0 = dindex - 0;
				xm1 = dindex - 1;
				xm2 = dindex - 2;
				xm3 = dindex - 3;
				cSamples[nout++] =
					(xm1 * xm2 * xm3 * c0 / -6.0 + xm0 * xm2 * xm3 * c1 / 2.0 +
					xm0 * xm1 * xm3 * c2 / -2.0 + xm0 * xm1 * xm2 * c3 / 6.0);
#endif
			out++;
			dindex += fdecim - 1;
			c0 = c1;
			c1 = c2;
			c2 = c3;
		}
		else {
			if (dindex > 2.5) printf ("Skip at %.2f\n", dindex);
			c0 = c1;
			c1 = c2;
			c2 = c3;
			dindex -= 1;
		}
	}
	//printf ("in %d out %d\n", in, out);
	return nout;
}
			
#define QUISK_NB_HWINDOW_SECS	500.E-6	// half-size of blanking window in seconds
static void NoiseBlanker(complex double * cSamples, int nSamples)
{
	static complex double * cSaved = NULL;
	static double  * dSaved = NULL;
	static double save_sum;
	static int save_size, hwindow_size, state, index, win_index;
	static int sample_rate = -1;
	int i, j, k, is_pulse;
	double mag, limit;
	complex double samp;
#if DEBUG
	static time_t time0 = 0;
	static int debug_count = 0;
#endif

	if (quisk_noise_blanker <= 0)
		return;
	if (quisk_sound_state.sample_rate != sample_rate) {	// Initialization
		sample_rate = quisk_sound_state.sample_rate;
		state = 0;
		index = 0;
		win_index = 0;
		save_sum = 0.0;
		hwindow_size = (int)(sample_rate * QUISK_NB_HWINDOW_SECS + 0.5);
		save_size = hwindow_size * 3;	// number of samples in the average
		i = save_size * sizeof(double);
		dSaved = (double *) realloc(dSaved, i);
		memset (dSaved, 0, i);
		i = save_size * sizeof(complex double);
		cSaved = (complex double *)realloc(cSaved, i);
		memset (cSaved, 0, i);
#if DEBUG
		printf ("Noise blanker: save_size %d  hwindow_size %d\n",
			save_size, hwindow_size);
#endif
	}
	switch(quisk_noise_blanker) {
	case 1:
	default:
		limit = 6.0;
		break;
	case 2:
		limit = 4.0;
		break;
	case 3:
		limit = 2.5;
		break;
	}
	for (i = 0; i < nSamples; i++) {
		// output oldest sample, save newest
		samp = cSamples[i];				// newest sample
		cSamples[i] = cSaved[index];	// oldest sample
		cSaved[index] = samp;
		// use newest sample
		mag = cabs(samp);
		save_sum -= dSaved[index];	// remove oldest sample magnitude
		dSaved[index] = mag;		// save newest sample magnitude
		save_sum += mag;			// update sum of samples
		if (mag <= save_sum / save_size * limit)	// see if we have a large pulse
			is_pulse = 0;
		else
			is_pulse = 1;
		switch (state) {
		case 0:		// Normal state
			if (is_pulse) {		// wait for a pulse
				state = 1;
				k = index;
				for (j = 0; j < hwindow_size; j++) {	// apply window to prior samples
					cSaved[k--] *= (double)j / hwindow_size;
					if (k < 0)
						k = save_size - 1;
				}
			}
			else if (win_index) {		// pulses have stopped, increase window to 1.0
				cSaved[index] *= (double)win_index / hwindow_size;
				if (++win_index >= hwindow_size)
					win_index = 0;	// no more window
			}
			break;
		case 1:		// we got a pulse
			cSaved[index] = 0;	// zero samples until the pulses stop
			if ( ! is_pulse) {
				// start raising the window, but be prepared to window another pulse
				state = 0;
				win_index = 1;
			}
			break;
		}
#if DEBUG
		if (debug_count) {
			printf ("%d", is_pulse);
			if (--debug_count == 0)
				printf ("\n");
		}
		else if (is_pulse && time(NULL) != time0) {
			time0 = time(NULL);
			debug_count = hwindow_size * 2;
			printf ("%d", is_pulse);
		}
#endif
		if (++index >= save_size)
			index = 0;
	}
	return;
}

#define NOTCH_DEBUG			0
#define NOTCH_DATA_SIZE					2048
#define NOTCH_FILTER_DESIGN_SIZE		NOTCH_DATA_SIZE / 4
#define NOTCH_FILTER_SIZE		(NOTCH_FILTER_DESIGN_SIZE - 1)
#define NOTCH_FILTER_FFT_SIZE	(NOTCH_FILTER_SIZE / 2 + 1)
#define NOTCH_DATA_START_SIZE	(NOTCH_FILTER_SIZE - 1)
#define NOTCH_DATA_OUTPUT_SIZE	(NOTCH_DATA_SIZE - NOTCH_DATA_START_SIZE)
#define NOTCH_FFT_SIZE			(NOTCH_DATA_SIZE / 2 + 1)
static void dAutoNotch(double * dsamples, int nSamples, int sidetone, int rate)
{
	int i, j, k, i1, i2, inp, signal, delta_sig, delta_i1, half_width;
	double d, d1, d2, avg;
	static int old1, count1, old2, count2;
	static int index;
	static fftw_plan planFwd=NULL;
	static fftw_plan planRev,fltrFwd, fltrRev;
	static double data_in[NOTCH_DATA_SIZE];
	static double data_out[NOTCH_DATA_SIZE];
	static complex double notch_fft[NOTCH_FFT_SIZE];
	static double fft_window[NOTCH_DATA_SIZE];
	static double fltr_in[NOTCH_DATA_SIZE];
	static double fltr_out[NOTCH_FILTER_DESIGN_SIZE];
	static complex double fltr_fft[NOTCH_FFT_SIZE];
	static double average_fft[NOTCH_FFT_SIZE];
	static int fltrSig;
#if NOTCH_DEBUG
	static char * txt;
	double dmax;
#endif

	if ( ! planFwd) {		// set up FFT plans
		planFwd = fftw_plan_dft_r2c_1d(NOTCH_DATA_SIZE, data_in, notch_fft, FFTW_MEASURE);
		planRev = fftw_plan_dft_c2r_1d(NOTCH_DATA_SIZE, notch_fft, data_out, FFTW_MEASURE);		// destroys notch_fft
		fltrFwd = fftw_plan_dft_r2c_1d(NOTCH_DATA_SIZE, fltr_in, fltr_fft, FFTW_MEASURE);
		fltrRev = fftw_plan_dft_c2r_1d(NOTCH_FILTER_DESIGN_SIZE, fltr_fft, fltr_out, FFTW_MEASURE);
		for (i = 0; i < NOTCH_FILTER_SIZE; i++)
			fft_window[i] = 0.50 - 0.50 * cos(2. * M_PI * i / (NOTCH_FILTER_SIZE));	// Hanning
			//fft_window[i] = 0.54 - 0.46 * cos(2. * M_PI * i / (NOTCH_FILTER_SIZE));	// Hamming
	}
	if ( ! dsamples) {		// initialize
		index = NOTCH_DATA_START_SIZE;
		fltrSig = -1;
		old1 = old2 = 0;
		count1 = count2 = -4;
		memset(data_out, 0, sizeof(double) * NOTCH_DATA_SIZE);
		memset(data_in, 0, sizeof(double) * NOTCH_DATA_SIZE);
		memset(average_fft, 0, sizeof(double) * NOTCH_FFT_SIZE);
		return;
	}
	if ( ! quisk_auto_notch)
		return;
	// index into FFT data = frequency * 2 * NOTCH_FFT_SIZE / rate
	// index into filter design = frequency * 2 * NOTCH_FILTER_FFT_SIZE / rate
	for (inp = 0; inp < nSamples; inp++) {
		data_in[index] = dsamples[inp];
		dsamples[inp] = data_out[index];
		if (++index >= NOTCH_DATA_SIZE) {	// we have a full FFT of samples
			index = NOTCH_DATA_START_SIZE;
			fftw_execute(planFwd);		// Calculate forward FFT
			// Find maximum FFT bins
			delta_sig = (300 * 2 * NOTCH_FFT_SIZE + rate / 2) / rate;	// small frequency interval
			delta_i1 = (400 * 2 * NOTCH_FFT_SIZE + rate / 2) / rate;	// small frequency interval
			if (sidetone != 0)			// For CW, accept a signal at the frequency of the RIT
				signal = (abs(sidetone) * 2 * NOTCH_FFT_SIZE + rate / 2) / rate;
			else
				signal = -999;
			avg = 1;
#if NOTCH_DEBUG
			dmax = 0;
#endif
			d1 = 0;
			i1 = 0;		// First maximum signal
			for (i = 0; i < NOTCH_FFT_SIZE; i++) {
				d = cabs(notch_fft[i]);
				avg += d;
				//average_fft[i] = 0.9 * average_fft[i] + 0.1 * d;
				average_fft[i] = 0.5 * average_fft[i] + 0.5 * d;
				if (abs(i - signal) > delta_sig && average_fft[i] > d1) {
					d1 = average_fft[i];
					i1 = i;
#if NOTCH_DEBUG
					dmax = d;
#endif
				}
			}
			if (abs(i1 - old1) < 3)		// See if the maximum bin i1 is changing
				count1++;
			else
				count1--;
			if (count1 > 4)
				count1 = 4;
			else if (count1 < -1)
				count1 = -1;
			if (count1 < 0)
				old1 = i1;
			avg /= NOTCH_FFT_SIZE;
			d2 = 0;
			i2 = 0;		// Next maximum signal not near the first
			for (i = 0; i < NOTCH_FFT_SIZE; i++) {
				if (abs(i - signal) > delta_sig && abs(i - i1) > delta_i1 && average_fft[i] > d2) {
					d2 = average_fft[i];
					i2 = i;
				}
			}
			if (abs(i2 - old2) < 3)		// See if the maximum bin i2 is changing
				count2++;
			else
				count2--;
			if (count2 > 4)
				count2 = 4;
			else if (count2 < -2)
				count2 = -2;
			if (count2 < 0)
				old2 = i2;

			if (count1 > 0 && count2 > 0)
				k = i1 + 10000 * i2;	// trial filter index
			else if(count1 > 0)
				k = i1;
			else
				k = 0;
			// Make the filter if it is different
			if (fltrSig != k) {
				fltrSig = k;
				half_width = (100 * 2 * NOTCH_FILTER_FFT_SIZE + rate / 2) / rate;	// half the width of the notch
				if (half_width < 3)
					half_width = 3;
				for (i = 0; i < NOTCH_FILTER_FFT_SIZE; i++)
					fltr_fft[i] = 1.0;
				k = (i1 + 2) / 4;		// Ratio of index values is 4
#if NOTCH_DEBUG
				txt = "Fxx";
#endif
				if (count1 > 0) {
#if NOTCH_DEBUG
					txt = "F1";
#endif
					for (i = -half_width; i <= half_width; i++) {
						j = k + i;
						if (j >= 0 && j < NOTCH_FILTER_FFT_SIZE)
							fltr_fft[j] = 0.0;
					}
				}
				k = (i2 + 2) / 4;		// Ratio of index values is 4
				if (count1 > 0 && count2 > 0) {
#if NOTCH_DEBUG
					txt = "F12";
#endif
					for (i = -half_width; i <= half_width; i++) {
						j = k + i;
						if (j >= 0 && j < NOTCH_FILTER_FFT_SIZE)
							fltr_fft[j] = 0.0;
					}
				}
				fftw_execute(fltrRev);
				// center the coefficient zero, make the filter symetric, reduce the size by one
				memmove(fltr_out + NOTCH_FILTER_DESIGN_SIZE / 2 - 1, fltr_out, sizeof(double) * (NOTCH_FILTER_SIZE / 2 - 1));
				for (i = NOTCH_FILTER_DESIGN_SIZE / 2 - 2, j = NOTCH_FILTER_DESIGN_SIZE / 2; i >= 0; i--, j++)
					fltr_out[i] = fltr_out[j];
				for (i = 0; i < NOTCH_FILTER_SIZE; i++)
					fltr_in[i] = fltr_out[i] * fft_window[i] / NOTCH_FILTER_DESIGN_SIZE;
				for (i = NOTCH_FILTER_SIZE; i < NOTCH_DATA_SIZE; i++)
					fltr_in[i] = 0.0;
				fftw_execute(fltrFwd);		// The filter is fltr_fft[]
			}
#if NOTCH_DEBUG
			printf("Max %12.0lf  frequency index1 %3d %5d %12.0lf  index2 %3d %5d %12.0lf  avg %12.0lf  %s\n", dmax, count1, i1, d1, count2, i2, d2, avg, txt);
#endif
			for (i = 0; i < NOTCH_FFT_SIZE; i++)	// Apply the filter
				notch_fft[i] *= fltr_fft[i];
			fftw_execute(planRev);		// Calculate inverse FFT
			memmove(data_in, data_in + NOTCH_DATA_OUTPUT_SIZE, NOTCH_DATA_START_SIZE * sizeof(double));
			for (i = NOTCH_DATA_START_SIZE; i < NOTCH_DATA_SIZE; i++)
				data_out[i] /= NOTCH_DATA_SIZE / 20;	// Empirical
		}
	}
	return;
}

#if 0
static complex double * audio_fft;
static int audio_fft_ready=0;
static void calc_audio_graph(double * dsamples, int nSamples)
{ // Calculate an FFT for the audio data
	int i, inp;
	static int index;
	static int audio_fft_size;
	static fftw_plan plan;
	static double * data_in;
	static double * fft_window;

	if ( ! dsamples) {		// malloc new space and initialize
		index = 0;
		audio_fft_size = data_width;
		data_in = (double *)malloc(audio_fft_size * sizeof(double));
		fft_window = (double *)malloc(audio_fft_size * sizeof(double));
		audio_fft = (complex double *)malloc((audio_fft_size / 2 + 1) * sizeof(complex double));
		plan = fftw_plan_dft_r2c_1d(audio_fft_size, data_in, audio_fft, FFTW_MEASURE);
		for (i = 0; i < audio_fft_size; i++)
			fft_window[i] = 0.50 - 0.50 * cos(2. * M_PI * i / audio_fft_size);	// Hanning
		return;
	}
	for (inp = 0; inp < nSamples; inp++) {
		data_in[index] = dsamples[inp];
		if (++index >= audio_fft_size) {	// we have a full FFT of samples
			index = 0;
			for (i = 0; i < audio_fft_size; i++)
				data_in[i] *= fft_window[i];	// multiply by window
			fftw_execute(plan);		// Calculate forward FFT
			audio_fft_ready = (audio_fft_ready + 1) & 0xff;
			//printf("Audio FFT %d %d %d\n", audio_fft_ready, audio_fft_size, data_width);
		}
	}
}

static PyObject * get_audio_graph(PyObject * self, PyObject * args)
{
	int i, j, k;
	static int seq = 0;
	double d2, scale;
	PyObject * tuple2;

	if (!PyArg_ParseTuple (args, ""))
		return NULL;

	if (seq == audio_fft_ready) {		// a new graph is not yet available
		Py_INCREF (Py_None);
		return Py_None;
	}
	seq = audio_fft_ready;
	tuple2 = PyTuple_New(data_width);
	scale = log10((double)data_width) + 31.0 * log10(2.0);
	scale *= 20.0;
	j = 0;
	k = data_width / 2 - 1;
	for (i = 0; i < data_width / 2; i++, k--) {
		d2 = 20.0 * log10(cabs(audio_fft[k])) - scale;
		if (d2 < -200)
			d2 = -200;
		PyTuple_SetItem(tuple2, j++, PyFloat_FromDouble(d2));
	}
	for (i = 0; i < data_width / 2; i++) {
		d2 = 20.0 * log10(cabs(audio_fft[i])) - scale;
		if (d2 < -200)
			d2 = -200;
		PyTuple_SetItem(tuple2, j++, PyFloat_FromDouble(d2));
	}
	if (j < data_width)
		PyTuple_SetItem(tuple2, j++, PyFloat_FromDouble(-200.0));
	return tuple2;
}
#else
static PyObject * get_audio_graph(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, ""))
		return NULL;

	Py_INCREF (Py_None);
	return Py_None;
}
#endif

static complex double dRxFilterOut(complex double sample, int bank)
{  // Rx FIR filter; bank is the static storage index, and must be different for different data streams
	complex double cx;
	int j, k;
	static int init = 0;
	static struct stStorage {
		int indexFilter;						// current index into sample buffer
		complex double bufFilterC[MAX_FILTER_SIZE];	// Digital filter sample buffer
	} Storage[2];
	struct stStorage * ptBuf = Storage + bank;

	if ( ! init) {
		init = 1;
		memset(Storage, 0, sizeof(struct stStorage));
		memset(Storage + 1, 0, sizeof(struct stStorage));
	}

	if ( ! sizeFilter)
		return sample;
	if (ptBuf->indexFilter >= sizeFilter)
		ptBuf->indexFilter = 0;
	ptBuf->bufFilterC[ptBuf->indexFilter] = sample;
	cx = 0;
	j = ptBuf->indexFilter;
	for (k = 0; k < sizeFilter; k++) {
		cx += ptBuf->bufFilterC[j] * cFilterI[k];
		if (++j >= sizeFilter)
			j = 0;
	}
	ptBuf->indexFilter++;
	return cx;
}

static complex double cRxFilterOut(complex double sample, int bank)
{  // Rx FIR filter; bank is the static storage index, and must be different for different data streams
	double accI, accQ;
	int j, k;
	static int init = 0;
	static struct stStorage {
		int indexFilter;						// current index into sample buffer
		double bufFilterI[MAX_FILTER_SIZE];		// Digital filter sample buffer
		double bufFilterQ[MAX_FILTER_SIZE];		// Digital filter sample buffer
	} Storage[2];
	struct stStorage * ptBuf = Storage + bank;

	if ( ! init) {
		init = 1;
		memset(Storage, 0, sizeof(struct stStorage));
		memset(Storage + 1, 0, sizeof(struct stStorage));
	}

	if ( ! sizeFilter)
		return sample;
	if (ptBuf->indexFilter >= sizeFilter)
		ptBuf->indexFilter = 0;
	ptBuf->bufFilterI[ptBuf->indexFilter] = creal(sample);
	ptBuf->bufFilterQ[ptBuf->indexFilter] = cimag(sample);
	accI = accQ = 0;
	j = ptBuf->indexFilter;
	for (k = 0; k < sizeFilter; k++) {
		accI += ptBuf->bufFilterI[j] * cFilterI[k];
		accQ += ptBuf->bufFilterQ[j] * cFilterQ[k];
		if (++j >= sizeFilter)
			j = 0;
	}
	ptBuf->indexFilter++;
	return accI + I * accQ;
}

static void AddTestTone(complex double * cSamples, int nSamples)
{
	int i;
	static complex double testtoneVector = 21474836.47;	// -40 dB
	static complex double audioVector = 1.0;
	complex double audioPhase;

	switch (rxMode) {
	default:
		//testtonePhase = cexp(I * 2 * M_PI * (quisk_sidetoneCtrl - 500) / 1000.0);
		for (i = 0; i < nSamples; i++) {
			cSamples[i] += testtoneVector;
			testtoneVector *= testtonePhase;
		}
		break;
	case 4:		// AM
		//audioPhase = cexp(I * 2 * M_PI * quisk_sidetoneCtrl * 5 / sample_rate);
		audioPhase = cexp(I * 2.0 * M_PI * 1000 / quisk_sound_state.sample_rate);
		for (i = 0; i < nSamples; i++) {
			cSamples[i] += testtoneVector * (1.0 + creal(audioVector));
			testtoneVector *= testtonePhase;
			audioVector *= audioPhase;
		}
		break;
	case 5:		 // FM
		//audioPhase = cexp(I * 2 * M_PI * quisk_sidetoneCtrl * 5 / sample_rate);
		audioPhase = cexp(I * 2.0 * M_PI * 1000 / quisk_sound_state.sample_rate);
		for (i = 0; i < nSamples; i++) {
			cSamples[i] += testtoneVector * cexp(I * creal(audioVector));
			testtoneVector *= testtonePhase;
			audioVector *= audioPhase;
		}
		break;
	}
}

static int IsSquelch(int freq)
{  // measure the signal level for squelch
	int i, i1, i2, iBandwidth;
	double meter;

	// This uses current_graph with width data_width
	iBandwidth = 5000 * data_width / fft_sample_rate;	// bandwidth determines number of pixels to average
	if (iBandwidth < 1)
		iBandwidth = 1;
	i1 = (int)((double)freq * data_width / fft_sample_rate + data_width / 2.0 - iBandwidth / 2.0 + 0.5);
	i2 = i1 + iBandwidth;
	meter = 0;
	if (i1 >= 0 && i2 < data_width) {	// too close to edge?
		for (i = i1; i < i2; i++)
			meter += current_graph[i];
	}
	meter /= iBandwidth;
	if (meter == 0 || meter < squelch_level)
		return 1;   // meter == 0 means Rx freq is off-screen so squelch is on
	else
		return 0;
}

static PyObject * set_record_state(PyObject * self, PyObject * args)
{  // called when a Record or Play button is pressed, or with -1 to poll
	int button;

	if (!PyArg_ParseTuple (args, "i", &button))
		return NULL;
	switch (button) {
	case 0:			// press record radio
	case 4:			// press record microphone
		if ( ! quisk_record_buffer) {	// initialize
			quisk_record_bufsize = (int)(QuiskGetConfigDouble("max_record_minutes", 0.25) * quisk_sound_state.playback_rate * 60.0 + 0.2);
			quisk_record_buffer = (float *)malloc(sizeof(float) * quisk_record_bufsize);
		}
		quisk_record_index = 0;
		quisk_play_index = 0;
		quisk_mic_index = 0;
		quisk_record_full = 0;
		if (button == 0)
			quisk_record_state = RECORD_RADIO;
		else
			quisk_record_state = RECORD_MIC;
		break;
	case 1:			// release record
		quisk_record_state = IDLE;
		break;
	case 2:			// press play
		if (quisk_record_full) {
			quisk_play_index = quisk_record_index + 1;
			if (quisk_play_index >= quisk_record_bufsize)
				quisk_play_index = 0;
		}
		else {
			quisk_play_index = 0;
		}
		quisk_mic_index = quisk_play_index;
		quisk_record_state = PLAYBACK;
		break;
	case 3:			// release play
		quisk_record_state = IDLE;
		break;
	}
	return PyInt_FromLong(quisk_record_state != PLAYBACK);
}

void quisk_tmp_record(complex double * cSamples, int nSamples, double scale)		// save sound
{
	int i;

	for (i = 0; i < nSamples; i++) {
		quisk_record_buffer[quisk_record_index++] = creal(cSamples[i]) * scale;
		if (quisk_record_index >= quisk_record_bufsize) {
			quisk_record_index = 0;
			quisk_record_full = 1;
		}
	}
}

static void tmp_playback(complex double * cSamples, int nSamples, double volume)
{  // replace radio sound with saved sound
	int i;
	double d;

	for (i = 0; i < nSamples; i++) {
		d = quisk_record_buffer[quisk_play_index++] * volume;
		cSamples[i] = d + I * d;
		if (quisk_play_index >= quisk_record_bufsize)
			quisk_play_index = 0;
		if (quisk_play_index == quisk_record_index) {
			quisk_record_state = IDLE;
			return;
		}
	}
}

void quisk_tmp_microphone(complex double * cSamples, int nSamples)
{  // replace microphone samples with saved sound
	int i;
	double d;

	for (i = 0; i < nSamples; i++) {
		d = quisk_record_buffer[quisk_mic_index++];
		cSamples[i] = d + I * d;
		if (quisk_mic_index >= quisk_record_bufsize)
			quisk_mic_index = 0;
		if (quisk_mic_index == quisk_record_index) {
			quisk_record_state = IDLE;
			return;
		}
	}
}

static int quisk_process_decimate(complex double * cSamples, int nSamples, int bank)
{
	int i, final_filter;
	static struct stStorage {
		struct quisk_cHB45Filter HalfBand1;
		struct quisk_cHB45Filter HalfBand2;
		struct quisk_cHB45Filter HalfBand3;
		struct quisk_cHB45Filter HalfBand4;
		struct quisk_cFilter filtSdriq111;
		struct quisk_cFilter filtSdriq53;
		struct quisk_cFilter filtSdriq133;
		struct quisk_cFilter filtSdriq167;
		struct quisk_cFilter filtSdriq185;
		struct quisk_cFilter filtDecim3;
		struct quisk_cFilter filtDecim5;
		struct quisk_cFilter filtDecim5S;
		struct quisk_cFilter filtDecim48to24;
	} Storage[2] ;

	if ( ! cSamples) {	// Initialize all filters
		for (i = 0; i < 2; i++) {
			memset(&Storage[i].HalfBand1, 0, sizeof(struct quisk_cHB45Filter));
			memset(&Storage[i].HalfBand2, 0, sizeof(struct quisk_cHB45Filter));
			memset(&Storage[i].HalfBand3, 0, sizeof(struct quisk_cHB45Filter));
			memset(&Storage[i].HalfBand4, 0, sizeof(struct quisk_cHB45Filter));
			quisk_filt_cInit(&Storage[i].filtSdriq111, quiskFilt111D2Coefs, sizeof(quiskFilt111D2Coefs)/sizeof(double));
			quisk_filt_cInit(&Storage[i].filtSdriq53, quiskFilt53D1Coefs, sizeof(quiskFilt53D1Coefs)/sizeof(double));
			quisk_filt_cInit(&Storage[i].filtSdriq133, quiskFilt133D2Coefs, sizeof(quiskFilt133D2Coefs)/sizeof(double));
			quisk_filt_cInit(&Storage[i].filtSdriq167, quiskFilt167D3Coefs, sizeof(quiskFilt167D3Coefs)/sizeof(double));
			quisk_filt_cInit(&Storage[i].filtSdriq185, quiskFilt185D3Coefs, sizeof(quiskFilt185D3Coefs)/sizeof(double));
			quisk_filt_cInit(&Storage[i].filtDecim3, quiskFilt144D3Coefs, sizeof(quiskFilt144D3Coefs)/sizeof(double));
			quisk_filt_cInit(&Storage[i].filtDecim5, quiskFilt240D5Coefs, sizeof(quiskFilt240D5Coefs)/sizeof(double));
			quisk_filt_cInit(&Storage[i].filtDecim5S, quiskFilt240D5CoefsSharp, sizeof(quiskFilt240D5CoefsSharp)/sizeof(double));
			quisk_filt_cInit(&Storage[i].filtDecim48to24, quiskFilt48dec24Coefs, sizeof(quiskFilt48dec24Coefs)/sizeof(double));
		}
		return 0;
	}
	// Decimate: Lower the sample rate to 48000 sps (or approx).  Filters are designed for
	// a pass bandwidth of 20 kHz and a stop bandwidth of 24 kHz.
	// We use 48 ksps to accommodate wide digital modes.
	final_filter = (rxMode == 7 || rxMode == 8 || rxMode == 9);	// Use sharp FIR final filter for decimate
	quisk_decim_srate = 48000;
	switch((quisk_sound_state.sample_rate + 100) / 1000) {
	case 41:
	case 48:
		break;
	case 53:	// SDR-IQ
		quisk_decim_srate = quisk_sound_state.sample_rate;
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtSdriq53, 1);
		break;
	case 96:
		if (final_filter)
			nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim48to24, 2);
		else
			nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand1);
		break;
	case 111:	// SDR-IQ
		quisk_decim_srate = quisk_sound_state.sample_rate / 2;
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtSdriq111, 2);
		break;
	case 133:	// SDR-IQ
		quisk_decim_srate = quisk_sound_state.sample_rate / 2;
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtSdriq133, 2);
		break;
	case 185:	// SDR-IQ
		quisk_decim_srate = quisk_sound_state.sample_rate / 3;
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtSdriq185, 3);
		break;
	case 192:
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand2);
		if (final_filter)
			nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim48to24, 2);
		else
			nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand1);
		break;
	case 240:
		if (final_filter)
			nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim5S, 5);
		else
			nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim5, 5);
		break;
    case 288:
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand2);
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim3, 3);
		break;
	case 370:
		quisk_decim_srate = quisk_sound_state.sample_rate / 6;
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand2);
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtSdriq185, 3);
		break;
	case 384:
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand2);
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand3);
		if (final_filter)
			nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim48to24, 2);
		else
			nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand1);
		break;
	case 480:
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim5, 5);
		if (final_filter)
			nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim48to24, 2);
		else
			nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand1);
		break;
	case 740:
		quisk_decim_srate = quisk_sound_state.sample_rate / 12;
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand2);
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand3);
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtSdriq185, 3);
		break;
    case 768:
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand2);
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand3);
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand4);
		if (final_filter)
			nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim48to24, 2);
		else
			nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand1);
		break;
	case 960:
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand2);
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim5, 5);
		if (final_filter)
			nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim48to24, 2);
		else
			nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand1);
		break;
    case 1152:
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand1);
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand2);
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand3);
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim3, 3);
		break;
	case 1333:
		quisk_decim_srate = quisk_sound_state.sample_rate / 24;
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand1);
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand2);
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand3);
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtSdriq167, 3);
		break;
    case 2304:
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand1);
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand2);
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand3);
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand4);
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim3, 3);
		break;
	default:
		printf ("Failure in quisk.c in integer decimation\n");
		break;
	}
	return nSamples;
}

static int quisk_process_demodulate(complex double * cSamples, double * dsamples, int nSamples, int bank)
{
	int i;
	complex double cx, cpx;
	double d, di, dd;
	static struct stStorage {
		complex double fm_1;			// Sample delayed by one
		double dc_remove;		// DC removal for AM
		double FM_www;
		double FM_nnn, FM_a_0, FM_a_1, FM_b_1, FM_x_1, FM_y_1;   // filter for FM
		struct quisk_cHB45Filter HalfBand4;
		struct quisk_cHB45Filter HalfBand5;
		struct quisk_dHB45Filter HalfBand6;
		struct quisk_dFilter filtAudio24p3;
		struct quisk_dFilter filtAudio24p4;
		struct quisk_dFilter filtAudio12p2;
		struct quisk_dFilter filtAudio24p6;
		struct quisk_dFilter filtAudioFmHp;
		struct quisk_cFilter filtDecim48to24;
	} Storage[2] ;

	if ( ! cSamples) {	// Initialize all filters
		for (i = 0; i < 2; i++) {
			memset(&Storage[i].HalfBand4, 0, sizeof(struct quisk_cHB45Filter));
			memset(&Storage[i].HalfBand5, 0, sizeof(struct quisk_cHB45Filter));
			memset(&Storage[i].HalfBand6, 0, sizeof(struct quisk_dHB45Filter));
			quisk_filt_dInit(&Storage[i].filtAudio24p3, quiskAudio24p3Coefs, sizeof(quiskAudio24p3Coefs)/sizeof(double));
			quisk_filt_dInit(&Storage[i].filtAudio24p4, quiskAudio24p4Coefs, sizeof(quiskAudio24p4Coefs)/sizeof(double));
			quisk_filt_dInit(&Storage[i].filtAudio12p2, quiskAudio24p4Coefs, sizeof(quiskAudio24p4Coefs)/sizeof(double));
			quisk_filt_dInit(&Storage[i].filtAudio24p6, quiskAudio24p6Coefs, sizeof(quiskAudio24p6Coefs)/sizeof(double));
			quisk_filt_dInit(&Storage[i].filtAudioFmHp, quiskAudioFmHpCoefs, sizeof(quiskAudioFmHpCoefs)/sizeof(double));
			quisk_filt_cInit(&Storage[i].filtDecim48to24, quiskFilt48dec24Coefs, sizeof(quiskFilt48dec24Coefs)/sizeof(double));
			Storage[i].fm_1 = 10;
			Storage[i].FM_www = tan(M_PI * FM_FILTER_DEMPH / 24000);   // filter for FM
			Storage[i].FM_nnn = 1.0 / (1.0 + Storage[i].FM_www);
			Storage[i].FM_a_0 = Storage[i].FM_www * Storage[i].FM_nnn;
			Storage[i].FM_a_1 = Storage[i].FM_a_0;
			Storage[i].FM_b_1 = Storage[i].FM_nnn * (Storage[i].FM_www - 1.0);
			//printf ("dsamples[i] = y_1 = di * %12.6lf + x_1 * %12.6lf - y_1 * %12.6lf\n", FM_a_0, FM_a_1, FM_b_1);
		}
		return 0;
	}

	// Filter and demodulate signal, copy capture buffer cSamples to play buffer dsamples.
	// quisk_decim_srate is the sample rate after integer decimation.
    quisk_demod_srate = quisk_decim_srate;
	switch(rxMode) {
	case 0:		// lower sideband CW at 6 ksps
		quisk_demod_srate /= 8;
		quisk_filter_srate = quisk_demod_srate;
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand5);
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand4);
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim48to24, 2);
		for (i = 0; i < nSamples; i++) {
			cx = cRxFilterOut(cSamples[i], bank);
			dsamples[i] = dd = creal(cx) + cimag(cx);
			measure_audio_sum += dd * dd;
			measure_audio_count += 1;
		}
		if(bank == 0)
			dAutoNotch(dsamples, nSamples, rit_freq, quisk_filter_srate);
		nSamples = quisk_dInterpolate(dsamples, nSamples, &Storage[bank].filtAudio12p2, 2);
		nSamples = quisk_dInterp2HB45(dsamples, nSamples, &Storage[bank].HalfBand6);
		quisk_demod_srate *= 4;
		break;
	case 1:		// upper sideband CW at 6 ksps
		quisk_demod_srate /= 8;
		quisk_filter_srate = quisk_demod_srate;
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand5);
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand4);
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim48to24, 2);
		for (i = 0; i < nSamples; i++) {
			cx = cRxFilterOut(cSamples[i], bank);
			dsamples[i] = dd = creal(cx) - cimag(cx);
			measure_audio_sum += dd * dd;
			measure_audio_count += 1;
		}
		if(bank == 0)
			dAutoNotch(dsamples, nSamples, rit_freq, quisk_filter_srate);
		nSamples = quisk_dInterpolate(dsamples, nSamples, &Storage[bank].filtAudio12p2, 2);
		nSamples = quisk_dInterp2HB45(dsamples, nSamples, &Storage[bank].HalfBand6);
		quisk_demod_srate *= 4;
		break;
	case 2:	 // lower sideband SSB at 12 ksps
		quisk_demod_srate /= 4;
		quisk_filter_srate = quisk_demod_srate;
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand5);
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim48to24, 2);
		for (i = 0; i < nSamples; i++) {
			cx = cRxFilterOut(cSamples[i], bank);
			dsamples[i] = dd = creal(cx) + cimag(cx);
			measure_audio_sum += dd * dd;
			measure_audio_count += 1;
		}
		if(bank == 0)
			dAutoNotch(dsamples, nSamples, 0, quisk_filter_srate);
		//calc_audio_graph(dsamples, nSamples);
		nSamples = quisk_dInterpolate(dsamples, nSamples, &Storage[bank].filtAudio24p4, 2);
		quisk_demod_srate *= 2;
		break;
	case 3:	 // upper sideband SSB at 12 ksps
	default:
		quisk_demod_srate /= 4;
		quisk_filter_srate = quisk_demod_srate;
		nSamples = quisk_cDecim2HB45(cSamples, nSamples, &Storage[bank].HalfBand5);
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim48to24, 2);
		for (i = 0; i < nSamples; i++) {
			cx = cRxFilterOut(cSamples[i], bank);
			dsamples[i] = dd = creal(cx) - cimag(cx);
			measure_audio_sum += dd * dd;
			measure_audio_count += 1;
		}
		if(bank == 0)
			dAutoNotch(dsamples, nSamples, 0, quisk_filter_srate);
		//calc_audio_graph(dsamples, nSamples);
		nSamples = quisk_dInterpolate(dsamples, nSamples, &Storage[bank].filtAudio24p4, 2);
		quisk_demod_srate *= 2;
		break;
	case 4:		// AM at 24 ksps
		quisk_demod_srate /= 2;
		quisk_filter_srate = quisk_demod_srate;
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim48to24, 2);
		for (i = 0; i < nSamples; i++) {
			cx = dRxFilterOut(cSamples[i], bank);
			di = cabs(cx);
			d = di + Storage[bank].dc_remove * 0.99;	// DC removal; R.G. Lyons page 553
			di = d - Storage[bank].dc_remove;
			Storage[bank].dc_remove = d;
			dsamples[i] = di;
			measure_audio_sum += di * di;
			measure_audio_count += 1;
		}
		nSamples = quisk_dFilter(dsamples, nSamples, &Storage[bank].filtAudio24p6);
		if(bank == 0)
			dAutoNotch(dsamples, nSamples, 0, quisk_filter_srate);
		break;
	case 5:		// FM at 24 ksps
		quisk_demod_srate /= 2;
		quisk_filter_srate = quisk_demod_srate;
		nSamples = quisk_cDecimate(cSamples, nSamples, &Storage[bank].filtDecim48to24, 2);
		for (i = 0; i < nSamples; i++) {
			cx = dRxFilterOut(cSamples[i], bank);
			cpx = cx * conj(Storage[bank].fm_1);
			Storage[bank].fm_1 = cx;
			di = quisk_demod_srate * carg(cpx);
			// FM de-emphasis
			dsamples[i] = dd = Storage[bank].FM_y_1 = di * Storage[bank].FM_a_0 +
				Storage[bank].FM_x_1 * Storage[bank].FM_a_1 - Storage[bank].FM_y_1 * Storage[bank].FM_b_1;
			Storage[bank].FM_x_1 = di;
			measure_audio_sum += dd * dd;
			measure_audio_count += 1;
		}
		nSamples = quisk_dDecimate(dsamples, nSamples, &Storage[bank].filtAudio24p3, 2);
		nSamples = quisk_dFilter(dsamples, nSamples, &Storage[bank].filtAudioFmHp);
		nSamples = quisk_dInterp2HB45(dsamples, nSamples, &Storage[bank].HalfBand6);
		if(bank == 0)
			dAutoNotch(dsamples, nSamples, 0, quisk_filter_srate);
		break;
	case 7:     // digital mode DGT-U at 48 ksps
		quisk_filter_srate = quisk_demod_srate;
		for (i = 0; i < nSamples; i++) {
			cx = cRxFilterOut(cSamples[i], bank);
			dsamples[i] = dd = creal(cx) - cimag(cx);
			measure_audio_sum += dd * dd;
			measure_audio_count += 1;
		}
		if(bank == 0)
			dAutoNotch(dsamples, nSamples, 0, quisk_filter_srate);
		break;
	case 8:     // digital mode DGT-L at 48 ksps
		quisk_filter_srate = quisk_demod_srate;
		for (i = 0; i < nSamples; i++) {
			cx = cRxFilterOut(cSamples[i], bank);
			dsamples[i] = dd = creal(cx) + cimag(cx);
			measure_audio_sum += dd * dd;
			measure_audio_count += 1;
		}
		if(bank == 0)
			dAutoNotch(dsamples, nSamples, 0, quisk_filter_srate);
		break;
	case 9:     // digital mode DGT-IQ at 48 ksps
		quisk_filter_srate = quisk_demod_srate;
		if (filter_bandwidth < 19000) {		// No filtering for wide bandwidth
			for (i = 0; i < nSamples; i++)
				cSamples[i] = dRxFilterOut(cSamples[i], bank);
			measure_audio_sum = measure_audio_sum + cSamples[i] * conj(cSamples[i]);
			measure_audio_count += 1;
		}
		break;
	}
	if (measure_audio_count >= quisk_filter_srate * measure_audio_time) {
		measured_audio = sqrt(measure_audio_sum / measure_audio_count) / CLIP32 * 1e6;
		measure_audio_sum = measure_audio_count = 0;
	}
	return nSamples;
}

static void process_agc(struct AgcState * dat, complex double * csamples, int count, int is_cpx)
{
	int i;
	double out_magn, buf_magn, dtmp, clip_gain;
	complex double csample;
#if DEBUG
	static int printit=0;
	static double maxout=1;
	char * clip;
#endif

	if ( ! dat->buf_size) {		// initialize
		dat->buf_size = quisk_sound_state.playback_rate * AGC_DELAY / 1000;	// total delay in samples
		//printf("play rate %d  buf_size %d\n", quisk_sound_state.playback_rate, dat->buf_size);
		dat->index_read = 0;				// Index to output; and then write a new sample here
		dat->index_start = 0;				// Start index for measure of maximum sample
		dat->is_clipping = 0;				// Are we decreasing gain to handle a clipping condition?
		dat->themax = 1.0;					// Maximum sample in the buffer
		dat->gain = 100;					// Current output gain
		dat->delta = 0;						// Amount to change dat->gain at each sample
		dat->target_gain = 100;				// Move to this gain unless we clip
		dat->c_samp = (complex double *) malloc(dat->buf_size * sizeof(complex double));		// buffer for complex samples
		for (i = 0; i < dat->buf_size; i++)
			dat->c_samp[i] = 0;
		return;
	}
	for (i = 0; i < count; i++) {
		csample = csamples[i];
		csamples[i] = dat->c_samp[dat->index_read] * dat->gain;		// FIFO output
		if (is_cpx)
			out_magn = cabs(csamples[i]);
		else
			out_magn = fabs(creal(csamples[i]));
		//if(dat->is_clipping == 1)
			//printf("      index %5d  out_magn %.5lf  gain %.2lf  delta  %.5lf\n",dat->index_read, out_magn / CLIP32, dat->gain, dat->delta);
#if DEBUG
		if (out_magn > maxout)
			maxout = out_magn;
#endif
		if (out_magn > CLIP32)	{
			csamples[i] /= out_magn;
#if DEBUG
			printf("Clip out_magn %8.5lf  is_clipping %d  index_read %5d  index_start %5d  gain %8.5lf\n",
				out_magn / CLIP32, dat->is_clipping, dat->index_read, dat->index_start, dat->gain);
#endif
		}
		dat->c_samp[dat->index_read] = csample;		// write new sample at read index
		if (is_cpx)
			buf_magn = cabs(csample);
		else
			buf_magn = fabs(creal(csample));
		if (dat->is_clipping == 0) {
			if (buf_magn * dat->gain > AGC_MAX_OUT) {
				dat->target_gain = AGC_MAX_OUT / buf_magn;
				dat->delta = (dat->gain - dat->target_gain) / dat->buf_size;
				dat->is_clipping = 1;
				dat->themax = buf_magn;
				// printf("Start index %5d  buf_magn %10.8lf  target %8.2lf  gain %8.2lf  delta  %8.5lf\n",
				// 	dat->index_read, buf_magn / CLIP32, dat->target_gain, dat->gain, dat->delta);
				dat->gain -= dat->delta;
			}
			else if (dat->index_read == dat->index_start) {
				clip_gain = AGC_MAX_OUT / dat->themax;		// clip gain based on the maximum sample in the buffer
				if (rxMode == 5)		// mode is FM
					dat->target_gain = clip_gain;
				else if (agcReleaseGain > clip_gain)
					dat->target_gain = clip_gain;
				else
					dat->target_gain = agcReleaseGain;
				dat->themax = buf_magn;
				dat->gain = dat->gain * (1.0 - agcTimeRelease) + dat->target_gain * agcTimeRelease;
				// printf("New   index %5d  themax %7.5lf  clip_gain %5.0lf  agcReleaseGain %5.0lf\n",
				// 	dat->index_start, dat->themax / CLIP32, clip_gain, agcReleaseGain);
			}
			else {
				if (dat->themax < buf_magn)
					dat->themax = buf_magn;
				dat->gain = dat->gain * (1.0 - agcTimeRelease) + dat->target_gain * agcTimeRelease;
			}
		}
		else {		// dat->is_clipping == 1;  we are handling a clip condition
			if (buf_magn > dat->themax) {
				dat->themax = buf_magn;
				dat->target_gain = AGC_MAX_OUT / buf_magn;
				dtmp = (dat->gain - dat->target_gain) / dat->buf_size;	// new value of delta
				if (dtmp > dat->delta) {
					dat->delta = dtmp;
					// printf(" Strt index %5d  buf_magn %10.8lf  target %8.2lf  gain %8.2lf  delta  %8.5lf\n",
					// 	dat->index_read, buf_magn / CLIP32, dat->target_gain, dat->gain, dat->delta);
				}
				else {
					// printf(" Plus index %5d  buf_magn %10.8lf  target %8.2lf  gain %8.2lf  delta  %8.5lf\n",
					// 	dat->index_read, buf_magn / CLIP32, dat->target_gain, dat->gain, dat->delta);
				}
			}
			dat->gain -= dat->delta;
			if (dat->gain <= dat->target_gain) {
				dat->is_clipping = 0;
				dat->gain = dat->target_gain;
				// printf("End   index %5d  buf_magn %10.8lf  target %8.2lf  gain %8.2lf  delta  %8.5lf  themax %10.8lf\n",
				// 	dat->index_read, buf_magn / CLIP32, dat->target_gain, dat->gain, dat->delta, dat->themax / CLIP32);
				dat->themax = buf_magn;
				dat->index_start = dat->index_read;
			}
		}
		if (++dat->index_read >= dat->buf_size)
			dat->index_read = 0;
#if DEBUG
		if (printit++ >= quisk_sound_state.playback_rate * 500 / 1000) {
			printit = 0;
			dtmp = 20 * log10(maxout / CLIP32);
			if (dtmp >= 0)
				clip = "Clip";
			else
				clip = "";
			printf("Out agcGain %5.0lf   target_gain %9.0lf   gain %9.0lf  output %7.2lf %s\n",
				agcReleaseGain, dat->target_gain, dat->gain, dtmp, clip);
			maxout = 1;
		}
#endif
	}
	return;
}

int quisk_process_samples(complex double * cSamples, int nSamples)
{
// Called when samples are available.
// Samples range from about 2^16 to a max of 2^31.
	int i, n, nout, is_key_down, interp;
	double d, di, tune;
	double double_filter_decim;
	complex double phase;
	int orig_nSamples;
	fft_data * ptFFT;

	static int size_dsamples = 0;		// Current dimension of dsamples, dsamples2, orig_cSamples
	static double * dsamples = NULL;
	static double * dsamples2 = NULL;
	static complex double * orig_cSamples = NULL;
	static complex double rxTuneVector = 1;
	static complex double txTuneVector = 1;
	static complex double sidetoneVector = BIG_VOLUME;
	static double dOutCounter = 0;		// Cumulative net output samples for sidetone etc.
	static int sidetoneIsOn = 0;		// The status of the sidetone
	static double sidetoneEnvelope;		// Shape the rise and fall times of the sidetone
	static double keyupEnvelope = 1.0;	// Shape the rise time on key up
	static int playSilence;
	static int is_squelch = 0;		// Are we squelched?
	static struct quisk_cHB45Filter HalfBand7 = {NULL, 0, 0};
	static struct quisk_cHB45Filter HalfBand8 = {NULL, 0, 0};
	static struct quisk_cHB45Filter HalfBand9 = {NULL, 0, 0};
	static struct AgcState Agc1 = {0}, Agc2 = {0};

#if DEBUG
	static int printit;
	static time_t time0;
	static double levelA=0, levelB=0, levelC=0, levelD=0, levelE=0;

	if (time(NULL) != time0) {
		time0 = time(NULL);
		printit = 1;
	}
	else {
		printit = 0;
	}
#endif
	if (nSamples <= 0)
		return nSamples;
	if (nSamples > size_dsamples) {
		if (dsamples)
			free(dsamples);
		if (dsamples2)
			free(dsamples2);
		if (orig_cSamples)
			free(orig_cSamples);
		size_dsamples = nSamples * 2;
		dsamples = (double *)malloc(size_dsamples * sizeof(double));
		dsamples2 = (double *)malloc(size_dsamples * sizeof(double));
		orig_cSamples = (complex double *)malloc(size_dsamples * sizeof(complex double));
	}
	is_key_down = quisk_transmit_mode || quisk_is_key_down();
	orig_nSamples = nSamples;
	if (split_rxtx)
		memcpy(orig_cSamples, cSamples, nSamples * sizeof(complex double));

	if (is_key_down && !isFDX) {	// The key is down; replace this data block
		dOutCounter += (double)nSamples * quisk_sound_state.playback_rate /
				quisk_sound_state.sample_rate;
		nout = (int)dOutCounter;			// number of samples to output
		dOutCounter -= nout;
		playSilence = keyupDelay;
		keyupEnvelope = 0;
		if (rxMode == 0 || rxMode == 1) {	// Play sidetone instead of radio for CW
			if (! sidetoneIsOn) {			// turn on sidetone
				sidetoneIsOn = 1;
				sidetoneEnvelope = 0;
				sidetoneVector = BIG_VOLUME;
			}
			for (i = 0 ; i < nout; i++) {
				if (sidetoneEnvelope < 1.0) {
					sidetoneEnvelope += 1. / (quisk_sound_state.playback_rate * 5e-3);	// 5 milliseconds
					if (sidetoneEnvelope > 1.0)
						sidetoneEnvelope = 1.0;
				}
				d = creal(sidetoneVector) * sidetoneVolume * sidetoneEnvelope;
				cSamples[i] = d + I * d;
				sidetoneVector *= sidetonePhase;
			}
		}
		else {			// Otherwise play silence
			for (i = 0 ; i < nout; i++)
				cSamples[i] = 0;
		}
		return nout;
	}
	// Key is up
	if(sidetoneIsOn) {		// decrease sidetone until it is off
		dOutCounter += (double)nSamples * quisk_sound_state.playback_rate /
				quisk_sound_state.sample_rate;
		nout = (int)dOutCounter;			// number of samples to output
		dOutCounter -= nout;
		for (i = 0; i < nout; i++) {
			sidetoneEnvelope -= 1. / (quisk_sound_state.playback_rate * 5e-3);	// 5 milliseconds
			if (sidetoneEnvelope < 0) {
				sidetoneIsOn = 0;
				sidetoneEnvelope = 0;
				break;		// sidetone is zero
			}
			d = creal(sidetoneVector) * sidetoneVolume * sidetoneEnvelope;
			cSamples[i] = d + I * d;
			sidetoneVector *= sidetonePhase;
		}
		for ( ; i < nout; i++) {	// continue with playSilence, even if zero
			cSamples[i] = 0;
			playSilence--;
		}
		return nout;
	}
	if (playSilence > 0) {		// Continue to play silence after the key is up
		dOutCounter += (double)nSamples * quisk_sound_state.playback_rate /
				quisk_sound_state.sample_rate;
		nout = (int)dOutCounter;			// number of samples to output
		dOutCounter -= nout;
		for (i = 0; i < nout; i++)
			cSamples[i] = 0;
		playSilence -= nout;
		return nout;
	}
	// We are done replacing sound with a sidetone or silence.  Filter and
	// demodulate the samples as radio sound.

	// Add a test tone to the data
	if (testtonePhase)
		AddTestTone(cSamples, nSamples);

	// Invert spectrum
	if (quisk_invert_spectrum) {
		for (i = 0; i < nSamples; i++) {
			cSamples[i] = conj(cSamples[i]);
			}
	}

	NoiseBlanker(cSamples, nSamples);

	// Put samples into the fft input array.
	// Thanks to WB4JFI for the code to add a third FFT buffer, July 2010.
	// Changed to multiple FFTs May 2014.
	if (multiple_sample_rates == 0) {
	    ptFFT = fft_data_array + fft_data_index;
	    for (i = 0; i < nSamples; i++) {
		    ptFFT->samples[ptFFT->index] = cSamples[i];
		    if (++(ptFFT->index) >= fft_size) {		// check sample count
			    n = fft_data_index + 1;				// next FFT data location
			    if (n >= FFT_ARRAY_SIZE)
				    n = 0;
			    if (fft_data_array[n].filled == 0) {				// Is the next buffer empty?
				    fft_data_array[n].index = 0;
				    fft_data_array[n].block = 0;
				    fft_data_array[fft_data_index].filled = 1;	// Mark the previous buffer ready.
				    fft_data_index = n;							// Write samples into the new buffer.
				    ptFFT = fft_data_array + fft_data_index;
			    }
			    else {				// no place to write samples
				    ptFFT->index = 0;
				    fft_error++;
			    }
		    }
	    }
	}

	// No need to tune and demodulate if we don't play sound
	if (quisk_sound_state.dev_play_name[0] == 0)
		return 0;
	// Tune the data to frequency
	if (multiple_sample_rates == 0)
        tune = rx_tune_freq;
    else
        tune = rx_tune_freq + vfo_screen - vfo_audio;
	if (tune != 0) {
		phase = cexp((I * -2.0 * M_PI * tune) / quisk_sound_state.sample_rate);
		for (i = 0; i < nSamples; i++) {
			cSamples[i] *= rxTuneVector;
			rxTuneVector *= phase;
		}
	}

	if (rxMode == 6) {		// External filter and demodulate
		d = (double)quisk_sound_state.sample_rate / quisk_sound_state.playback_rate;	// total decimation needed
		nSamples = quisk_extern_demod(cSamples, nSamples, d);
		goto start_agc;
	}

	// Perhaps write sample data to the soundcard output without decimation
	if (TEST_AUDIO == 1) {		// Copy I channel capture to playback
		di = 1.e4 * quisk_audioVolume;
		for (i = 0; i < nSamples; i++)
			cSamples[i] = creal(cSamples[i]) * di;
		return nSamples;
	}
	else if (TEST_AUDIO == 2) {	// Copy Q channel capture to playback
		di = 1.e4 * quisk_audioVolume;
		for (i = 0; i < nSamples; i++)
			cSamples[i] = cimag(cSamples[i]) * di;
		return nSamples;
	}
#if DEBUG
	for (i = 0; i < nSamples; i++) {
		d = cabs(cSamples[i]);
		if (levelA < d)
			levelA = d;
	}
#endif

	nSamples = quisk_process_decimate(cSamples, nSamples, 0);

#if DEBUG
	for (i = 0; i < nSamples; i++) {
		d = cabs(cSamples[i]);
		if (levelB < d)
			levelB = d;
	}
#endif

	if (measure_freq_mode)
		measure_freq(cSamples, nSamples, quisk_decim_srate);

	nSamples = quisk_process_demodulate(cSamples, dsamples, nSamples, 0);

	if (rxMode == 9) {
		;		// This mode is already stereo
	}
	else if (split_rxtx) {		// Demodulate a second channel
		phase = cexp((I * -2.0 * M_PI * quisk_tx_tune_freq) / quisk_sound_state.sample_rate);
		// Tune the second channel to frequency
		for (i = 0; i < orig_nSamples; i++) {
			orig_cSamples[i] *= txTuneVector;
			txTuneVector *= phase;
		}
		n = quisk_process_decimate(orig_cSamples, orig_nSamples, 1);
		n = quisk_process_demodulate(orig_cSamples, dsamples2, n, 1);
		// We assume that n == nSamples
		switch(split_rxtx) {
		default:
		case 1:		// stereo, higher frequency is real
			if (quisk_tx_tune_freq < rx_tune_freq) {
				for (i = 0; i < nSamples; i++)
					cSamples[i] = dsamples[i] + I * dsamples2[i];
			}
			else {
				for (i = 0; i < nSamples; i++)
					cSamples[i] = dsamples2[i] + I * dsamples[i];
			}
			break;
		case 2:		// stereo, lower frequency is real
			if (quisk_tx_tune_freq >= rx_tune_freq) {
				for (i = 0; i < nSamples; i++)
					cSamples[i] = dsamples[i] + I * dsamples2[i];
			}
			else {
				for (i = 0; i < nSamples; i++)
					cSamples[i] = dsamples2[i] + I * dsamples[i];
			}
			break;
		case 3:		// mono receive channel
			for (i = 0; i < nSamples; i++)
				cSamples[i] = dsamples[i] + I * dsamples[i];
			break;
		case 4:		// mono transmit channel
			for (i = 0; i < nSamples; i++)
				cSamples[i] = dsamples2[i] + I * dsamples2[i];
			break;
		}
	}
	else {		// monophonic sound played on both channels
		for (i = 0; i < nSamples; i++) {
			d = dsamples[i];
			cSamples[i] = d + I * d;
		}
	}

	// Perhaps decimate by an additional fraction
	if (quisk_decim_srate != 48000) {
		double_filter_decim = quisk_decim_srate / 48000.0;
		nSamples = cFracDecim(cSamples, nSamples, double_filter_decim);
		quisk_demod_srate = (int)(quisk_demod_srate / double_filter_decim + 0.5);
	}

	// Interpolate the samples back to the play rate.
	// quisk_demod_srate is either 24000 or 48000 +/- clock tolerance.
	quisk_demod_srate = ((quisk_demod_srate + 12000) / 24000) * 24000;
	interp = quisk_sound_state.playback_rate / quisk_demod_srate;
	if (interp > 1) {
		nSamples = quisk_cInterp2HB45(cSamples, nSamples, &HalfBand7);
		interp /= 2;
	}
	if (interp > 1) {
		nSamples = quisk_cInterp2HB45(cSamples, nSamples, &HalfBand8);
		interp /= 2;
	}
	if (interp > 1) {
		nSamples = quisk_cInterp2HB45(cSamples, nSamples, &HalfBand9);
		interp /= 2;
	}
	if (interp != 1)
		printf ("Failure in quisk.c in integer interpolation\n");

	// Find the peak signal amplitude
start_agc:
	if (rxMode == 6 || rxMode == 9) {		// DGT-IQ stereo sound
		process_agc(&Agc1, cSamples, nSamples, 1);
	}
	else if (split_rxtx) {		// separate AGC for left and right channels
		for (i = 0; i < nSamples; i++) {
			orig_cSamples[i] = cimag(cSamples[i]);
			cSamples[i] = creal(cSamples[i]);
		}
		process_agc(&Agc1, cSamples, nSamples, 0);
		process_agc(&Agc2, orig_cSamples, nSamples, 0);
		for (i = 0; i < nSamples; i++)
			cSamples[i] = creal(cSamples[i]) + I * creal(orig_cSamples[i]);
	}
	else {					// monophonic sound
		process_agc(&Agc1, cSamples, nSamples, 0);
	}
#if DEBUG
	if (printit) {
		d = CLIP32;
		//printf ("Levels: %12.8lf  %12.8lf %12.8lf  %12.8lf  %12.8lf\n",
		//	levelA/d, levelB/d, levelC/d, levelD/d, levelE/d);
		levelA = levelB = levelC = levelD = levelE = 0;
	}
#endif

	if (kill_audio) {
		is_squelch = 1;
	}
	else if (rxMode == 5) {		// mode is FM
		if (IsSquelch(quisk_tx_tune_freq))
			is_squelch = 1;
		else
			is_squelch = 0;
	}
	else {
		is_squelch = 0;
	}
	if (is_squelch) {
		for (i = 0; i < nSamples; i++)
			cSamples[i] = 0;
	}
	if (keyupEnvelope < 1.0) {		// raise volume slowly after the key goes up
		di = 1. / (quisk_sound_state.playback_rate * 5e-3);		// 5 milliseconds
		for (i = 0; i < nSamples; i++) {
			keyupEnvelope += di;
			if (keyupEnvelope > 1.0) {
				keyupEnvelope = 1.0;
				break;
			}
			cSamples[i] *= keyupEnvelope;
		}
	}
	if (quisk_record_state == RECORD_RADIO && ! is_squelch)
		quisk_tmp_record(cSamples, nSamples, 1.0);		// save radio sound
	if (quisk_record_state == PLAYBACK)
		tmp_playback(cSamples, nSamples, 1.0);	// replace radio sound
	return nSamples;
}

static PyObject * get_state(PyObject * self, PyObject * args)
{
	int unused = 0;

	if (args && !PyArg_ParseTuple (args, ""))	// args=NULL internal call
		return NULL;
	return  Py_BuildValue("iiiiisisiiiiiiiii",
		quisk_sound_state.rate_min,
		quisk_sound_state.rate_max,
		quisk_sound_state.sample_rate,
		quisk_sound_state.chan_min,
		quisk_sound_state.chan_max,
		&quisk_sound_state.msg1,
		unused,
		&quisk_sound_state.err_msg,
		quisk_sound_state.read_error,
		quisk_sound_state.write_error,
		quisk_sound_state.underrun_error,
		quisk_sound_state.latencyCapt,
		quisk_sound_state.latencyPlay,
		quisk_sound_state.interupts,
		fft_error,
		mic_max_display,
		quisk_sound_state.data_poll_usec
		);
}

static PyObject * get_squelch(PyObject * self, PyObject * args)
{
	int freq;

	if (!PyArg_ParseTuple (args, "i", &freq))
		return NULL;
	return PyInt_FromLong(IsSquelch(freq));
}

static PyObject * get_overrange(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, ""))
		return NULL;
	return PyInt_FromLong(quisk_get_overrange());
}

static PyObject * get_filter_rate(PyObject * self, PyObject * args)
{  // return the filter sample rate as used by quisk_process_samples
	complex double cSamples[2];
	double dsamples[2];
	if (!PyArg_ParseTuple (args, ""))
		return NULL;
	// run some fake data through the filters to calculate the rates
	quisk_process_decimate(cSamples, 0, 0);
	quisk_process_demodulate(cSamples, dsamples, 0, 0);
	return PyInt_FromLong(quisk_filter_srate);
}

static PyObject * get_smeter(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, ""))
		return NULL;
	return PyFloat_FromDouble(Smeter);
}

static PyObject * measure_frequency(PyObject * self, PyObject * args)
{
	int mode;

	if (!PyArg_ParseTuple (args, "i", &mode))
		return NULL;
	if (mode >= 0)	// mode >= 0 set the mode; mode < 0, just return the frequency
		measure_freq_mode = mode;
	return PyFloat_FromDouble(measured_frequency);
}

static PyObject * measure_audio(PyObject * self, PyObject * args)
{
	int time;

	if (!PyArg_ParseTuple (args, "i", &time))
		return NULL;
	if (time > 0)	// set the average time
		measure_audio_time = time;
	return PyFloat_FromDouble(measured_audio);
}

static PyObject * add_tone(PyObject * self, PyObject * args)
{  /* Add a test tone to the captured audio data */
	int freq;

	if (!PyArg_ParseTuple (args, "i", &freq))
		return NULL;
	if (freq && quisk_sound_state.sample_rate)
		testtonePhase = cexp((I * 2.0 * M_PI * freq) / quisk_sound_state.sample_rate);
	else
		testtonePhase = 0;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * open_key(PyObject * self, PyObject * args)
{
	const char * name;

	if (!PyArg_ParseTuple (args, "s", &name))
		return NULL;

	return PyInt_FromLong(quisk_open_key(name));
}

static void close_udp(void)
{
	short msg = 0x7373;		// shutdown

	quisk_using_udp = 0;
	if (rx_udp_socket != INVALID_SOCKET) {
		shutdown(rx_udp_socket, QUISK_SHUT_RD);
		send(rx_udp_socket, (char *)&msg, 2, 0);
		send(rx_udp_socket, (char *)&msg, 2, 0);
		QuiskSleepMicrosec(3000000);
		close(rx_udp_socket);
		rx_udp_socket = INVALID_SOCKET;
	}
	rx_udp_started = 0;
#ifdef MS_WINDOWS
	if (cleanupWSA) {
		cleanupWSA = 0;
		WSACleanup();
	}
#endif
}

static PyObject * close_rx_udp(PyObject * self, PyObject * args)
{  // Not necessary to call from Python because close_udp() is called from sound.c
	if (!PyArg_ParseTuple (args, ""))
		return NULL;
	close_udp();
	Py_INCREF (Py_None);
	return Py_None;
}

static int quisk_read_rx_udp(complex double * samp)	// Read samples from UDP
{		// Size of complex sample array is SAMP_BUFFER_SIZE
	ssize_t bytes;
	unsigned char buf[1500];	// Maximum Ethernet is 1500 bytes.
	static unsigned char seq0;	// must be 8 bits
	int i, nSamples, xr, xi, index, want_samples;
	unsigned char * ptxr, * ptxi;
	struct timeval tm_wait;
	fd_set fds;
	static complex double dc_average = 0;		// Average DC component in samples
	static complex double dc_sum = 0;
	static int dc_count = 0;
	static int dc_key_delay = 0;

	// Data from the receiver is little-endian
	if ( ! rx_udp_gain_correct) {
		int dec;
		dec = (int)(rx_udp_clock / quisk_sound_state.sample_rate + 0.5);
		if ((dec / 5) * 5 == dec)		// Decimation by a factor of 5
			rx_udp_gain_correct = 1.31072;
		else						// Decimation by factors of two
			rx_udp_gain_correct = 1.0;
	}
	if ( ! rx_udp_started) {	// we never received any data
		// send our return address until we receive UDP blocks
		tm_wait.tv_sec = 0;
		tm_wait.tv_usec = 5000;
		FD_ZERO (&fds);
		FD_SET (rx_udp_socket, &fds);
		if (select (rx_udp_socket + 1, &fds, NULL, NULL, &tm_wait) == 1) {	// see if data is available
			bytes = recv(rx_udp_socket, (char *)buf, 1500,  0);	// throw away the first block
			seq0 = buf[0] + 1;	// Next expected sequence number
			rx_udp_started = 1;
#if DEBUG_IO
			printf("Udp data started\n");
#endif
		}
		else {		// send our return address to the sample source
			buf[0] = buf[1] = 0x72;	// UDP command "register return address"
			send(rx_udp_socket, (char *)buf, 2, 0);
			return 0;
		}
	}
	nSamples = 0;
	want_samples = (int)(quisk_sound_state.data_poll_usec * 1e-6 * quisk_sound_state.sample_rate + 0.5);
	while (nSamples < want_samples) {		// read several UDP blocks
		tm_wait.tv_sec = 0;
		tm_wait.tv_usec = 100000; // Linux seems to have problems with very small time intervals
		FD_ZERO (&fds);
		FD_SET (rx_udp_socket, &fds);
		i = select (rx_udp_socket + 1, &fds, NULL, NULL, &tm_wait);
		if (i == 1)
			;
		else if (i == 0) {
#if DEBUG_IO
			printf("Udp socket timeout\n");
#endif
			return 0;
		}
		else {
#if DEBUG_IO
			printf("Udp select error %d\n", i);
#endif
			return 0;
		}
		bytes = recv(rx_udp_socket, (char *)buf, 1500,  0);	// blocking read
		if (bytes != RX_UDP_SIZE) {		// Known size of sample block
			quisk_sound_state.read_error++;
#if DEBUG_IO
			printf("read_rx_udp: Bad block size\n");
#endif
			continue;
		}
		// buf[0] is the sequence number
		// buf[1] is the status:
		//		bit 0:  key up/down state
		//		bit 1:	set for ADC overrange (clip)
		if (buf[0] != seq0) {
#if DEBUG_IO
			printf("read_rx_udp: Bad sequence want %3d got %3d\n",
					(unsigned int)seq0, (unsigned int)buf[0]);
#endif
			quisk_sound_state.read_error++;
		}
		seq0 = buf[0] + 1;		// Next expected sequence number
		quisk_set_key_down(buf[1] & 0x01);	// bit zero is key state
		if (buf[1] & 0x02)					// bit one is ADC overrange
			quisk_sound_state.overrange++;
		index = 2;
		ptxr = (unsigned char *)&xr;
		ptxi = (unsigned char *)&xi;
		// convert 24-bit samples to 32-bit samples; int must be 32 bits.
		if (is_little_endian) {
			while (index < bytes) {
				xr = xi = 0;
				memcpy (ptxr + 1, buf + index, 3);
				index += 3;
				memcpy (ptxi + 1, buf + index, 3);
				index += 3;
				samp[nSamples++] = (xr + xi * I) * rx_udp_gain_correct;
				xr = xi = 0;
				memcpy (ptxr + 1, buf + index, 3);
				index += 3;
				memcpy (ptxi + 1, buf + index, 3);
				index += 3;
				samp[nSamples++] = (xr + xi * I) * rx_udp_gain_correct;
	//if (nSamples == 2) printf("%12d %12d\n", xr, xi);
			}
		}
		else {		// big-endian
			while (index < bytes) {
				*(ptxr    ) = buf[index + 2];
				*(ptxr + 1) = buf[index + 1];
				*(ptxr + 2) = buf[index    ];
				*(ptxr + 3) = 0;
				index += 3;
				*(ptxi    ) = buf[index + 2];
				*(ptxi + 1) = buf[index + 1];
				*(ptxi + 2) = buf[index    ];
				*(ptxi + 3) = 0;
				index += 3;
				samp[nSamples++] = (xr + xi * I) * rx_udp_gain_correct;;
				*(ptxr    ) = buf[index + 2];
				*(ptxr + 1) = buf[index + 1];
				*(ptxr + 2) = buf[index    ];
				*(ptxr + 3) = 0;
				index += 3;
				*(ptxi    ) = buf[index + 2];
				*(ptxi + 1) = buf[index + 1];
				*(ptxi + 2) = buf[index    ];
				*(ptxi + 3) = 0;
				index += 3;
				samp[nSamples++] = (xr + xi * I) * rx_udp_gain_correct;;

			}
		}
	}
	if (quisk_is_key_down()) {
		dc_key_delay = 0;
		dc_sum = 0;
		dc_count = 0;
	}
	else if (dc_key_delay < quisk_sound_state.sample_rate) {
		dc_key_delay += nSamples;
	}
	else {
		dc_count += nSamples;
		for (i = 0; i < nSamples; i++)		// Correction for DC offset in samples
			dc_sum += samp[i];
		if (dc_count > quisk_sound_state.sample_rate * 2) {
			dc_average = dc_sum / dc_count;
			dc_sum = 0;
			dc_count = 0;
			//printf("dc average %lf   %lf %d\n", creal(dc_average), cimag(dc_average), dc_count);
			//printf("dc polar %.0lf   %d\n", cabs(dc_average),
			   		//	(int)(360.0 / 2 / M_PI * atan2(cimag(dc_average), creal(dc_average))));
		}
	}
	if (use_remove_dc)
		for (i = 0; i < nSamples; i++)	// Correction for DC offset in samples
			samp[i] -= dc_average;
	return nSamples;
}

static int read_rx_udp17(complex double * cSamples0)	// Read samples from UDP
{		// Size of complex sample array is SAMP_BUFFER_SIZE
	ssize_t bytes;
	unsigned char buf[1500];	// Maximum Ethernet is 1500 bytes.
	static unsigned char seq0;	// must be 8 bits
	int i, n, nSamples0, xr, xi, index, want_samples, key_down;
	complex double sample;
	unsigned char * ptxr, * ptxi;
	struct timeval tm_wait;
	fft_data * ptFFT;
	fd_set fds;
	static complex double dc_average = 0;		// Average DC component in samples
	static complex double dc_sum = 0;
	static int dc_count = 0;
	static int dc_key_delay = 0;
	static int block_number=0;
	// Data from the receiver is little-endian

	if ( ! rx_udp_gain_correct) {	// correct for second stage CIC decimation JIM JIM
		int dec;
		dec = (int)(rx_udp_clock / 30.0 / fft_sample_rate + 0.5);
		if ((dec / 3) * 3 == dec)		// Decimation by a factor of 3
			rx_udp_gain_correct = 1.053497942;
		else						// Decimation by factors of two
			rx_udp_gain_correct = 1.0;
		//printf ("Gain %d %.8lf\n", dec, rx_udp_gain_correct);
	}
	if ( ! rx_udp_started) {	// we never received any data
		// send our return address until we receive UDP blocks
		tm_wait.tv_sec = 0;
		tm_wait.tv_usec = 5000;
		FD_ZERO (&fds);
		FD_SET (rx_udp_socket, &fds);
		if (select (rx_udp_socket + 1, &fds, NULL, NULL, &tm_wait) == 1) {	// see if data is available
			bytes = recv(rx_udp_socket, (char *)buf, 1500,  0);	// throw away the first block
			seq0 = buf[0] + 1;	// Next expected sequence number
			rx_udp_started = 1;
#if DEBUG_IO
			printf("Udp data started\n");
#endif
		}
		else {		// send our return address to the sample source
			buf[0] = buf[1] = 0x72;	// UDP command "register return address"
			send(rx_udp_socket, (char *)buf, 2, 0);
			return 0;
		}
	}
	nSamples0 = 0;
	want_samples = (int)(quisk_sound_state.data_poll_usec * 1e-6 * quisk_sound_state.sample_rate + 0.5);
	key_down = quisk_is_key_down();
	while (nSamples0 < want_samples) {		// read several UDP blocks
		tm_wait.tv_sec = 0;
		tm_wait.tv_usec = 100000; // Linux seems to have problems with very small time intervals
		FD_ZERO (&fds);
		FD_SET (rx_udp_socket, &fds);
		i = select (rx_udp_socket + 1, &fds, NULL, NULL, &tm_wait);
		if (i == 1)
			;
		else if (i == 0) {
#if DEBUG_IO
			printf("Udp socket timeout\n");
#endif
			return 0;
		}
		else {
#if DEBUG_IO
			printf("Udp select error %d\n", i);
#endif
			return 0;
		}
		bytes = recv(rx_udp_socket, (char *)buf, 1500,  0);	// blocking read
		if (bytes != RX_UDP_SIZE) {		// Known size of sample block
			quisk_sound_state.read_error++;
#if DEBUG_IO
			printf("read_rx_udp: Bad block size\n");
#endif
			continue;
		}
		// buf[0] is the sequence number
		// buf[1] is the status:
		//		bit 0:  key up/down state
		//		bit 1:	set for ADC overrange (clip)
		if (buf[0] != seq0) {
#if DEBUG_IO
			printf("read_rx_udp: Bad sequence want %3d got %3d\n",
					(unsigned int)seq0, (unsigned int)buf[0]);
#endif
			quisk_sound_state.read_error++;
		}
		seq0 = buf[0] + 1;		// Next expected sequence number
		//quisk_set_key_down(buf[1] & 0x01);	// bit zero is key state
		if (buf[1] & 0x02)					// bit one is ADC overrange
			quisk_sound_state.overrange++;
		index = 2;
		ptxr = (unsigned char *)&xr;
		ptxi = (unsigned char *)&xi;
		// convert 24-bit samples to 32-bit samples; int must be 32 bits.
		while (index < bytes) {
			if (is_little_endian) {
				xr = xi = 0;
				memcpy (ptxr + 1, buf + index, 3);
				index += 3;
				memcpy (ptxi + 1, buf + index, 3);
				index += 3;
				sample = (xr + xi * I) * rx_udp_gain_correct;
			}
			else {		// big-endian
				*(ptxr    ) = buf[index + 2];
				*(ptxr + 1) = buf[index + 1];
				*(ptxr + 2) = buf[index    ];
				*(ptxr + 3) = 0;
				index += 3;
				*(ptxi    ) = buf[index + 2];
				*(ptxi + 1) = buf[index + 1];
				*(ptxi + 2) = buf[index    ];
				*(ptxi + 3) = 0;
				index += 3;
				sample = (xr + xi * I) * rx_udp_gain_correct;
			}
			if (xr & 0x100) {		// channel 1
				if (quisk_invert_spectrum)		// Invert spectrum
					sample = conj(sample);
				// Put samples into the fft input array.
				ptFFT = fft_data_array + fft_data_index;
				if ( ! (xi & 0x100)) {		// zero marker for start of first block
					if (ptFFT->index != 0) {
						//printf("Resync block\n");
						fft_error++;
						ptFFT->index = 0;
					}
					ptFFT->block = block_number = 0;
				}
				else if (ptFFT->index == 0) {
					if (scan_blocks) {
						if (++block_number < scan_blocks)
							ptFFT->block = block_number;
						else
							ptFFT->block = block_number = 0;
					}
					else {
						ptFFT->block = block_number = 0;
					}
					if (scan_blocks && block_number >= scan_blocks)
						printf("Bad block_number %d\n", block_number);
				}
				ptFFT->samples[ptFFT->index] = sample;
				if ((isFDX || ! key_down) && ++(ptFFT->index) >= fft_size) {		// check sample count
					n = fft_data_index + 1;				// next FFT data location
					if (n >= FFT_ARRAY_SIZE)
						n = 0;
					if (fft_data_array[n].filled == 0) {				// Is the next buffer empty?
						fft_data_array[n].index = 0;
						fft_data_array[n].block = 0;
						fft_data_array[fft_data_index].filled = 1;	// Mark the previous buffer ready.
						fft_data_index = n;							// Write samples into the new buffer.
						ptFFT = fft_data_array + fft_data_index;
					}
					else {				// no place to write samples
						ptFFT->index = 0;
						ptFFT->block = 0;
						fft_error++;
					}
				}
			}
			else {					// channel 0
				cSamples0[nSamples0++] = sample;
			}
		}
	}
	if (key_down) {
		dc_key_delay = 0;
		dc_sum = 0;
		dc_count = 0;
	}
	else if (dc_key_delay < quisk_sound_state.sample_rate) {
		dc_key_delay += nSamples0;
	}
	else {
		dc_count += nSamples0;
		for (i = 0; i < nSamples0; i++)		// Correction for DC offset in samples
			dc_sum += cSamples0[i];
		if (dc_count > quisk_sound_state.sample_rate * 2) {
			dc_average = dc_sum / dc_count;
			dc_sum = 0;
			dc_count = 0;
			//printf("dc average %lf   %lf %d\n", creal(dc_average), cimag(dc_average), dc_count);
			//printf("dc polar %.0lf   %d\n", cabs(dc_average),
			   		//	(int)(360.0 / 2 / M_PI * atan2(cimag(dc_average), creal(dc_average))));
		}
	}
	if (use_remove_dc)
		for (i = 0; i < nSamples0; i++)	// Correction for DC offset in samples
			cSamples0[i] -= dc_average;
	return nSamples0;
}

static PyObject * open_rx_udp(PyObject * self, PyObject * args)
{
	const char * ip;
	int port;
	char buf[128];
	struct sockaddr_in Addr;
	int recvsize;

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

	if (!PyArg_ParseTuple (args, "si", &ip, &port))
		return NULL;
#ifdef MS_WINDOWS
	wVersionRequested = MAKEWORD(2, 2);
	if (WSAStartup(wVersionRequested, &wsaData) != 0) {
		sprintf(buf, "Failed to initialize Winsock (WSAStartup)");
		return PyString_FromString(buf);
	}
	else {
		cleanupWSA = 1;
	}
#endif
	quisk_using_udp = 1;
	rx_udp_socket = socket(PF_INET, SOCK_DGRAM, 0);
	if (rx_udp_socket != INVALID_SOCKET) {
		recvsize = 256000;
		setsockopt(rx_udp_socket, SOL_SOCKET, SO_RCVBUF, (char *)&recvsize, sizeof(recvsize));
		memset(&Addr, 0, sizeof(Addr)); 
		Addr.sin_family = AF_INET;
		Addr.sin_port = htons(port);
#ifdef MS_WINDOWS
		Addr.sin_addr.S_un.S_addr = inet_addr(ip);
#else
		inet_aton(ip, &Addr.sin_addr);
#endif
		if (connect(rx_udp_socket, (const struct sockaddr *)&Addr, sizeof(Addr)) != 0) {
			shutdown(rx_udp_socket, QUISK_SHUT_BOTH);
			close(rx_udp_socket);
			rx_udp_socket = INVALID_SOCKET;
			sprintf(buf, "Failed to connect to UDP %s port 0x%X", ip, port);
		}
		else {
			sprintf(buf, "Capture from UDP %s port 0x%X", ip, port);
			if (use_rx_udp == 17)
				quisk_sample_source(NULL, close_udp, read_rx_udp17);
			else
				quisk_sample_source(NULL, close_udp, quisk_read_rx_udp);
#if DEBUG_IO
			if (getsockopt(rx_udp_socket, SOL_SOCKET, SO_RCVBUF, (char *)&intbuf, &bufsize) == 0)
				printf("UDP socket receive buffer size %d\n", intbuf);
			else
				printf ("Failure SO_RCVBUF\n");
#endif
		}
	}
	else {
		sprintf(buf, "Failed to open socket");
	}
	return PyString_FromString(buf);
}

static PyObject * open_sound(PyObject * self, PyObject * args)
{
    int rate;
	char * capt, * play, * mname, * mip, * mpname;
    double dtmp;

	if (!PyArg_ParseTuple (args, "ssiiissiiiidsi", &capt, &play,
				&rate,
				&quisk_sound_state.data_poll_usec,
				&quisk_sound_state.latency_millisecs,
				&mname, &mip,
				&quisk_sound_state.tx_audio_port,
				&quisk_sound_state.mic_sample_rate,
				&quisk_sound_state.mic_channel_I,
				&quisk_sound_state.mic_channel_Q,
				&quisk_sound_state.mic_out_volume,
				&mpname,
				&quisk_sound_state.mic_playback_rate
				))
		return NULL;
	if (quisk_sound_state.mic_out_volume > 0.7)	// maximum value must leave headroom for
		quisk_sound_state.mic_out_volume = 0.7;	//   the amplitude and phase adjustments
	quisk_sound_state.playback_rate = QuiskGetConfigInt("playback_rate", 48000);
	quisk_mic_preemphasis = QuiskGetConfigDouble("mic_preemphasis", 0.6);
	//if (quisk_mic_preemphasis < 0.0 || quisk_mic_preemphasis > 1.0)
	//	quisk_mic_preemphasis = 1.0;
	quisk_mic_clip = QuiskGetConfigDouble("mic_clip", 3.0);
	strncpy(quisk_sound_state.dev_capt_name, capt, QUISK_SC_SIZE);
	strncpy(quisk_sound_state.dev_play_name, play, QUISK_SC_SIZE);
	strncpy(quisk_sound_state.mic_dev_name, mname, QUISK_SC_SIZE);
	strncpy(quisk_sound_state.name_of_mic_play, mpname, QUISK_SC_SIZE);
	strncpy(quisk_sound_state.mic_ip, mip, IP_SIZE);
	fft_error = 0;
	quisk_open_sound();
	quisk_open_mic();
	dtmp = QuiskGetConfigDouble("agc_release_time", 1.0);
	agcTimeRelease = 1.0 - exp( - 1.0 / quisk_sound_state.playback_rate / dtmp);	// long time constant for AGC release
	return get_state(NULL, NULL);
}

static PyObject * close_sound(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, ""))
		return NULL;
	quisk_close_mic();
	quisk_close_sound();
	quisk_close_key();
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * change_scan(PyObject * self, PyObject * args)	// Called from GUI thread
{	// Change to a new FFT rate

	if (!PyArg_ParseTuple (args, "iidii", &scan_blocks, &scan_sample_rate, &scan_valid, &scan_vfo0, &scan_deltaf))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * change_rates(PyObject * self, PyObject * args)	// Called from GUI thread
{	// Change to new sample rates

    multiple_sample_rates = 1;
	if (!PyArg_ParseTuple (args, "iiii", &quisk_sound_state.sample_rate, &vfo_audio, &fft_sample_rate, &vfo_screen))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * change_rate(PyObject * self, PyObject * args)	// Called from GUI thread
{	// Change to a new sample rate
	int rate, avg;
    double d;

	if (!PyArg_ParseTuple (args, "ii", &rate, &avg))
		return NULL;
	if (multiple_sample_rates) {
		fft_sample_rate = rate;
	}
	else {
		quisk_sound_state.sample_rate = rate;
		fft_sample_rate = rate;
	}
	if (scan_blocks > 1) {
		d = (double)scan_sample_rate / scan_blocks;	// effective rate for graph refresh
		d = d / graph_refresh / fft_size;
		if (d < 1.0)
			d = 1.0;
		average_count = (int)(d + 0.5);
	}
    else {
	    average_count = avg;
    }
	rx_udp_gain_correct = 0;	// re-calculate JIM
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * read_sound(PyObject * self, PyObject * args)
{
	int n;

	if (!PyArg_ParseTuple (args, ""))
		return NULL;
Py_BEGIN_ALLOW_THREADS
	n = quisk_read_sound();
Py_END_ALLOW_THREADS
	return PyInt_FromLong(n);
}

static PyObject * start_sound(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, ""))
		return NULL;
	quisk_start_sound();
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * mixer_set(PyObject * self, PyObject * args)
{
	char * card_name;
	int numid;
	PyObject * value;
	char err_msg[QUISK_SC_SIZE];

	if (!PyArg_ParseTuple (args, "siO", &card_name, &numid, &value))
		return NULL;

	quisk_mixer_set(card_name, numid, value, err_msg, QUISK_SC_SIZE);
	return PyString_FromString(err_msg);
}

static PyObject * invert_spectrum(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, "i", &quisk_invert_spectrum))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_agc(PyObject * self, PyObject * args)
{  /* Change the AGC level */
	if (!PyArg_ParseTuple (args, "d", &agcReleaseGain))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_filters(PyObject * self, PyObject * args)
{  // Enter the coefficients of the I and Q digital filters.  The storage for
   // filters is not malloc'd because filters may be changed while being used.
	PyObject * filterI, * filterQ;
	int i, size;
	PyObject * obj;
	char buf98[98];

	if (!PyArg_ParseTuple (args, "OOi", &filterI, &filterQ, &filter_bandwidth))
		return NULL;
	if (PySequence_Check(filterI) != 1) {
		PyErr_SetString (QuiskError, "Filter I is not a sequence");
		return NULL;
	}
	if (PySequence_Check(filterQ) != 1) {
		PyErr_SetString (QuiskError, "Filter Q is not a sequence");
		return NULL;
	}
	size = PySequence_Size(filterI);
	if (size != PySequence_Size(filterQ)) {
		PyErr_SetString (QuiskError, "The size of filters I and Q must be equal");
		return NULL;
	}
	if (size >= MAX_FILTER_SIZE) {
		snprintf(buf98, 98, "Filter size must be less than %d", MAX_FILTER_SIZE);
		PyErr_SetString (QuiskError, buf98);
		return NULL;
	}
	for (i = 0; i < size; i++) {
		obj = PySequence_GetItem(filterI, i);
		cFilterI[i] = PyFloat_AsDouble(obj);
		Py_XDECREF(obj);
		obj = PySequence_GetItem(filterQ, i);
		cFilterQ[i] = PyFloat_AsDouble(obj);
		Py_XDECREF(obj);
	}
	sizeFilter = size;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_auto_notch(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, "i", &quisk_auto_notch))
		return NULL;
	dAutoNotch(NULL, 0, 0, 0);
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_noise_blanker(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, "i", &quisk_noise_blanker))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_rx_mode(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, "i", &rxMode))
		return NULL;
	quisk_set_tx_mode();
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_spot_level(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, "i", &quiskSpotLevel))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_mic_out_volume(PyObject * self, PyObject * args)
{
	int level;

	if (!PyArg_ParseTuple (args, "i", &level))
		return NULL;
	quisk_sound_state.mic_out_volume = level / 100.0;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_split_rxtx(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, "i", &split_rxtx))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_tune(PyObject * self, PyObject * args)
{  /* Change the tuning frequency */
	if (!PyArg_ParseTuple (args, "ii", &rx_tune_freq, &quisk_tx_tune_freq))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_sidetone(PyObject * self, PyObject * args)
{
	double delay;	// play extra silence after key-up, in milliseconds

	if (!PyArg_ParseTuple (args, "iid", &quisk_sidetoneCtrl, &rit_freq, &delay))
		return NULL;
	//printf("Sidetone control times 5 = %d\n", quisk_sidetoneCtrl * 5);
	// Simulate log taper pot
	sidetoneVolume = (exp(quisk_sidetoneCtrl * 0.006908) - 1) / 1000.0;
	sidetonePhase = cexp((I * 2.0 * M_PI * abs(rit_freq)) / quisk_sound_state.playback_rate);
	keyupDelay = (int)(quisk_sound_state.playback_rate *1e-3 * delay + 0.5);
	if (rxMode == 0 || rxMode == 1)
		dAutoNotch(NULL, 0, 0, 0);		// for CW, changing the RIT affects autonotch
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_squelch(PyObject * self, PyObject * args)
{  /* Change the squelch parameter */
	if (!PyArg_ParseTuple (args, "d", &squelch_level))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_kill_audio(PyObject * self, PyObject * args)
{	/* replace radio sound with silence */
	if (!PyArg_ParseTuple (args, "i", &kill_audio))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_transmit_mode(PyObject * self, PyObject * args)
{	/* Set the radio to transmit mode */
	if (!PyArg_ParseTuple (args, "i", &quisk_transmit_mode))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_volume(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, "d", &quisk_audioVolume))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_key_down(PyObject * self, PyObject * args)
{
	int down;

	if (!PyArg_ParseTuple (args, "i", &down))
		return NULL;
	quisk_set_key_down(down);
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * get_graph(PyObject * self, PyObject * args)	// Called by the GUI thread
{
	int i, j, k, m, n, index, ffts, ii, mm, m0, deltam;
	fft_data * ptFft;
	PyObject * tuple2;
	double d1, d2, scale, zoom, deltaf;
	complex double c;
	static double meter = 0;		// RMS s-meter
	static int use_fft = 1;			// Use the FFT, or return raw data
	static double * fft_avg=NULL;	// Array to average the FFT
	static double * fft_tmp;
	static int count_fft=0;			// how many fft's have occurred (for average)

	if (!PyArg_ParseTuple (args, "idd", &k, &zoom, &deltaf))
		return NULL;
	if (k != use_fft) {		// change in data return type; re-initialize
		use_fft = k;
		count_fft = 0;
	}
	if ( ! fft_avg) {
		fft_avg = (double *) malloc(sizeof(double) * fft_size);
		fft_tmp = (double *) malloc(sizeof(double) * fft_size);
		for (i = 0; i < fft_size; i++)
			fft_avg[i] = 0;
	}
	// Process all FFTs that are ready to run.
	index = fft_data_index;		// oldest data first - FIFO
	for (ffts = 0; ffts < FFT_ARRAY_SIZE; ffts++) {
		if (++index >= FFT_ARRAY_SIZE)
			index = 0;
		if (fft_data_array[index].filled)
			ptFft = fft_data_array + index;
		else
			continue;
		if (scan_blocks && ptFft->block >= scan_blocks) {
			//printf("Reject block %d\n", ptFft->block);
			ptFft->filled = 0;
			continue;
		}
		if ( ! use_fft) {		// return raw data, not FFT
			use_remove_dc = 0;	// No DC removal when returning raw data
			tuple2 = PyTuple_New(data_width);
			for (i = 0; i < data_width; i++)
				PyTuple_SetItem(tuple2, i,
					PyComplex_FromDoubles(creal(ptFft->samples[i]), cimag(ptFft->samples[i])));
			ptFft->filled = 0;
			return tuple2;
		}
		use_remove_dc = 1;
		// Continue with FFT calculation
		for (i = 0; i < fft_size; i++)		// multiply by window
			ptFft->samples[i] *= fft_window[i];
		fftw_execute(ptFft->plan);	// Calculate FFT
		// Create RMS s-meter value at known bandwidth
		// d2 is the number of FFT bins required for the bandwidth
		// i is the starting bin number from  - sample_rate / 2 to + sample_rate / 2
		if (scan_blocks)
			d1 = ((double)quisk_tx_tune_freq + vfo_screen - scan_vfo0 - scan_deltaf * ptFft->block) * fft_size / scan_sample_rate;
		else
			d1 = (double)quisk_tx_tune_freq * fft_size / fft_sample_rate;
//if (abs(k) < fft_size / 2 * scan_valid)
//printf("block %d at %d\n", ptFft->block, k);
		d2 = (double)filter_bandwidth * fft_size / fft_sample_rate;
		n = (int)(floor(d2) + 0.01);		// number of whole bins to add
		switch(rxMode) {
		default:	// CWL, CWU, AM, FM, DGT-IQ:  signal centered in bandwidth
			i = (int)(d1 - d2 / 2 + 0.5);
			break;
		case 2:		// LSB:  bandwidth is below tx frequency
		case 8:
			i = (int)(d1 - d2 + 0.5);
			break;
		case 3:		// USB:  bandwidth is above tx frequency
		case 7:
			i = (int)(d1 + 0.5);
			break;
		}
		if (i > - fft_size / 2 && i + n + 1 < fft_size / 2) {	// too close to edge?
			for (j = 0; j < n; i++, j++) {
				if (i < 0)
					c = ptFft->samples[fft_size + i];	// negative frequencies
				else
					c = ptFft->samples[i];				// positive frequencies
				meter = meter + c * conj(c);		// add square of amplitude
			}
			if (i < 0)			// add fractional next bin
				c = ptFft->samples[fft_size + i];
			else
				c = ptFft->samples[i];
			meter = meter + c * conj(c) * (d2 - n);	// fractional part of next bin
		}
		// Average the fft data into the graph in order of frequency
		if (scan_blocks) {
			if (ptFft->block == (scan_blocks - 1))
				count_fft++;
			k = 0;
			for (i = fft_size / 2; i < fft_size; i++)			// Negative frequencies
				fft_tmp[k++] = cabs(ptFft->samples[i]);
			for (i = 0; i < fft_size / 2; i++)					// Positive frequencies
				fft_tmp[k++] = cabs(ptFft->samples[i]);
			// Average this block into its correct position
			m0 = (int)(fft_size * ((1.0 - scan_valid) / 2.0));
			deltam = (int)(fft_size * scan_valid / scan_blocks);
			m = mm = m0 + ptFft->block * deltam;						// target position
			i = ii = (int)(fft_size * ((1.0 - scan_valid) / 2.0));	// start of valid data
			for (j = 0; j < deltam; j++) {
				d2 = 0;
				for (n = 0; n < scan_blocks; n++)
					d2 += fft_tmp[i++];
				fft_avg[m++] = d2;
			}
			//printf(" %d %.4lf At %5d to %5d place %5d to %5d for block %d\n", fft_size, scan_valid, mm, m, ii, i, ptFft->block);
		}
		else {
			count_fft++;
			k = 0;
			for (i = fft_size / 2; i < fft_size; i++)			// Negative frequencies
				fft_avg[k++] += cabs(ptFft->samples[i]);
			for (i = 0; i < fft_size / 2; i++)					// Positive frequencies
				fft_avg[k++] += cabs(ptFft->samples[i]);
		}
		ptFft->filled = 0;
		if (count_fft >= average_count) {
			count_fft = 0;
			// We have averaged enough fft's to return the graph data.
			// Average the fft data of size fft_size into the size of data_width.
			n = (int)(zoom * (double)fft_size / data_width + 0.5);
			if (n < 1)
				n = 1;
			for (i = 0; i < data_width; i++) {	// For each graph pixel
				// find k, the starting index into the FFT data
				k = (int)(fft_size * (
					deltaf / fft_sample_rate + zoom * ((double)i / data_width - 0.5) + 0.5) + 0.1);
				d2 = 0.0;
				for (j = 0; j < n; j++, k++)
					if (k >= 0 && k < fft_size)
						d2 += fft_avg[k];
				fft_avg[i] = d2;
			}
			scale = 1.0 / 2147483647.0 / fft_size;
			Smeter = meter * scale * scale / average_count;		// record the new s-meter value
			meter = 0;
			if (Smeter > 0)
				Smeter = 10.0 * log10(Smeter);
			else
				Smeter = -160.0;
			// This correction is for a -40 dB strong signal, and is caused by FFT leakage
			// into adjacent bins. It is the amplitude that is spread out, not the squared amplitude.
			Smeter += 4.25969;
			tuple2 = PyTuple_New(data_width);
			// scale = 1.0 / average_count / fft_size;	// Divide by sample count
			// scale /= pow(2.0, 31);			// Normalize to max == 1
			scale = log10(average_count) + log10(fft_size) + 31.0 * log10(2.0);
			scale *= 20.0;
			for (i = 0; i < data_width; i++) {
				d2 = 20.0 * log10(fft_avg[i]) - scale;
				if (d2 < -200)
					d2 = -200;
				current_graph[i] = d2;
				PyTuple_SetItem(tuple2, i, PyFloat_FromDouble(d2));
			}
			for (i = 0; i < fft_size; i++)
				fft_avg[i] = 0;
			return tuple2;
		}
	}
	Py_INCREF(Py_None);	// No data yet
	return Py_None;
}

static PyObject * get_filter(PyObject * self, PyObject * args)
{
	int i, j, k, n;
	int freq, time;
	PyObject * tuple2;
	complex double cx;
	double d2, scale, accI, accQ;
	double * average, * bufI, * bufQ;
	fft_data * FFT;
	fftw_complex * pt;
	double phase, delta;

	if (!PyArg_ParseTuple (args, ""))
		return NULL;

	// Create space for the fft of size data_width
	FFT = (fft_data *)malloc(sizeof(fft_data));
	FFT->filled = 0;
	FFT->index = 0;
	pt = FFT->samples = (fftw_complex *) fftw_malloc(sizeof(fftw_complex) * data_width);
	FFT->plan = fftw_plan_dft_1d(data_width, pt, pt, FFTW_FORWARD, FFTW_MEASURE);
	average = (double *) malloc(sizeof(double) * (data_width + sizeFilter));
	bufI = (double *) malloc(sizeof(double) * sizeFilter);
	bufQ = (double *) malloc(sizeof(double) * sizeFilter);

	for (i = 0; i < data_width + sizeFilter; i++)
		average[i] = 0.5;	// Value for freq == 0
	for (freq = 1; freq < data_width / 2.0 - 10.0; freq++) {
		delta = 2 * M_PI / data_width * freq;
		phase = 0;
		// generate some initial samples to fill the filter pipeline
		for (time = 0; time < data_width + sizeFilter; time++) {
			average[time] += cos(phase);	// current sample
			phase += delta;
			if (phase > 2 * M_PI)
				phase -= 2 * M_PI;
		}
	}
	// now filter the signal
	n = 0;
	for (time = 0; time < data_width + sizeFilter; time++) {
		d2 = average[time];
		bufI[n] = d2;
		bufQ[n] = d2;
		accI = accQ = 0;
		j = n;
		for (k = 0; k < sizeFilter; k++) {
			accI += bufI[j] * cFilterI[k];
			accQ += bufQ[j] * cFilterQ[k];
			if (++j >= sizeFilter)
				j = 0;
		}
		cx = accI + I * accQ;	// Filter output
		if (++n >= sizeFilter)
			n = 0;
		if (time >= sizeFilter)
			FFT->samples[time - sizeFilter] = cx;
	}

	for (i = 0; i < data_width; i++)	// multiply by window
		FFT->samples[i] *= fft_window[i];
	fftw_execute(FFT->plan);		// Calculate FFT
	// Normalize and convert to log10
	scale = 1. / data_width;
	for (k = 0; k < data_width; k++) {
		cx = FFT->samples[k];
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
	fftw_destroy_plan(FFT->plan);
	fftw_free(FFT->samples);
	free(FFT);

	return tuple2;
}

static void measure_freq(complex double * cSamples, int nSamples, int srate)
{
	int i, k, center, ipeak;
	double dmax, c3, freq;
	complex double cBuffer[SAMP_BUFFER_SIZE];
	static int index = 0;				// current index of samples
	static int fft_size=12000;			// size of fft data
	static int fft_count=0;				// number of ffts for the average
	static fftw_complex * samples;		// complex data for fft
	static fftw_plan planA;				// fft plan for fft
	static double * fft_window;			// window function
	static double * fft_average;		// average amplitudes
	static struct quisk_cHB45Filter HalfBand1 = {NULL, 0, 0};
	static struct quisk_cHB45Filter HalfBand2 = {NULL, 0, 0};
	static struct quisk_cHB45Filter HalfBand3 = {NULL, 0, 0};

	if ( ! cSamples) {		// malloc new space and initialize
		samples = (fftw_complex *) fftw_malloc(sizeof(fftw_complex) * fft_size);
		planA = fftw_plan_dft_1d(fft_size, samples, samples, FFTW_FORWARD, FFTW_MEASURE);
		fft_window = (double *) malloc(sizeof(double) * (fft_size + 1));
		fft_average = (double *) malloc(sizeof(double) * fft_size);
		memset(fft_average, 0, sizeof(double) * fft_size);
		for (i = 0; i < fft_size; i++)	// Hanning
			fft_window[i] = 0.50 - 0.50 * cos(2. * M_PI * i / (fft_size - 1));
		return;
	}
	memcpy(cBuffer, cSamples, nSamples * sizeof(complex double));	// do not destroy cSamples
	nSamples = quisk_cDecim2HB45(cBuffer, nSamples, &HalfBand1);
	nSamples = quisk_cDecim2HB45(cBuffer, nSamples, &HalfBand2);
	nSamples = quisk_cDecim2HB45(cBuffer, nSamples, &HalfBand3);
	srate /= 8;		// sample rate as decimated
	for (i = 0; i < nSamples && index < fft_size; i++, index++)
		samples[index] = cBuffer[i];
	if (index < fft_size)
		return;		// wait for a full array of samples
	for (i = 0; i < fft_size; i++)	// multiply by window
		samples[i] *= fft_window[i];
	fftw_execute(planA);		// Calculate FFT
	index = 0;
	fft_count++;
	// Average the fft data into the graph in order of frequency
	k = 0;
	for (i = fft_size / 2; i < fft_size; i++)			// Negative frequencies
		fft_average[k++] += cabs(samples[i]);
	for (i = 0; i < fft_size / 2; i++)					// Positive frequencies
		fft_average[k++] += cabs(samples[i]);
	if (fft_count < measure_freq_mode / 2)
		return;		// continue with average
	// time for a calculation
	fft_count = 0;
	dmax = 1.e-20;
	ipeak = 0;
	center = fft_size / 2 - rit_freq * fft_size / srate;
	k = 500;	// desired +/- half-bandwidth to search for a peak
	k = k * fft_size / srate;		// convert to index
	for (i = center - k; i <= center + k; i++) {	// search for a peak near the RX freq
		if (fft_average[i] > dmax) {
			dmax = fft_average[i];
			ipeak = i;
		}
	}
	c3 = 1.36 * (fft_average[ipeak+1] - fft_average[ipeak - 1]) / (fft_average[ipeak-1] + fft_average[ipeak] + fft_average[ipeak+1]);
	freq = srate * (2 * (ipeak + c3) - fft_size) / 2 / fft_size;
	freq += rx_tune_freq;
	//printf("freq %.0f rx_tune_freq %d vfo_screen %d vfo_audio %d\n", freq, rx_tune_freq, vfo_screen, vfo_audio);
	// printf("\n%5d %.4lf %.2lf k=%d\n", ipeak, c3, freq, k);
	measured_frequency = freq;
	//for (i = ipeak - 10; i <= ipeak + 10 && i >= 0 && i < fft_size; i++)
	//	printf("%4d %12.5f\n", i, fft_average[i] / dmax);
	memset(fft_average, 0, sizeof(double) * fft_size);
}

static PyObject * Xdft(PyObject * pyseq, int inverse, int window)
{  // Native spectral order is 0 Hz to (Fs - 1).  Change this to
   // - (Fs - 1)/2 to + Fs/2.  For even Fs==32, there are 15 negative
   // frequencies, a zero, and 16 positive frequencies.  For odd Fs==31,
   // there are 15 negative and positive frequencies plus zero frequency.
   // Note that zero frequency is always index (Fs - 1) / 2.
	PyObject * obj;
	int i, j, size;
	static int fft_size = -1;			// size of fft data
	static fftw_complex * samples;		// complex data for fft
	static fftw_plan planF, planB;		// fft plan for fftW
	static double * fft_window;			// window function
	Py_complex pycx;					// Python C complex value

	if (PySequence_Check(pyseq) != 1) {
		PyErr_SetString (QuiskError, "DFT input data is not a sequence");
		return NULL;
	}
	size = PySequence_Size(pyseq);
	if (size <= 0)
		return PyTuple_New(0);
	if (size != fft_size) {		// Change in previous size; malloc new space
		if (fft_size > 0) {
			fftw_destroy_plan(planF);
			fftw_destroy_plan(planB);
			fftw_free(samples);
			free (fft_window);
		}
		fft_size = size;	// Create space for one fft
		samples = (fftw_complex *) fftw_malloc(sizeof(fftw_complex) * fft_size);
		planF = fftw_plan_dft_1d(fft_size, samples, samples, FFTW_FORWARD, FFTW_MEASURE);
		planB = fftw_plan_dft_1d(fft_size, samples, samples, FFTW_BACKWARD, FFTW_MEASURE);
		fft_window = (double *) malloc(sizeof(double) * (fft_size + 1));
		for (i = 0; i <= size/2; i++) {
			if (1)	// Blackman window
				fft_window[i] = fft_window[size - i] = 0.42 + 0.50 * cos(2. * M_PI * i / size) + 
					0.08 * cos(4. * M_PI * i / size);
			else if (1)	// Hamming
				fft_window[i] = fft_window[size - i] = 0.54 + 0.46 * cos(2. * M_PI * i / size);
			else	// Hanning
				fft_window[i] = fft_window[size - i] = 0.50 + 0.50 * cos(2. * M_PI * i / size);
		}
	}
	j = (size - 1) / 2;		// zero frequency in input
	for (i = 0; i < size; i++) {
		obj = PySequence_GetItem(pyseq, j);
		if (PyComplex_Check(obj)) {
			pycx = PyComplex_AsCComplex(obj);
		}
		else if (PyFloat_Check(obj)) {
			pycx.real = PyFloat_AsDouble(obj);
			pycx.imag = 0;
		}
		else if (PyInt_Check(obj)) {
			pycx.real = PyInt_AsLong(obj);
			pycx.imag = 0;
		}
		else {
			Py_XDECREF(obj);
			PyErr_SetString (QuiskError, "DFT input data is not a complex/float/int number");
			return NULL;
		}
		samples[i] = pycx.real + I * pycx.imag;
		if (++j >= size)
			j = 0;
		Py_XDECREF(obj);
	}
	if (inverse) {		// Normalize using 1/N
		fftw_execute(planB);		// Calculate inverse FFT / N
		if (window) {
			for (i = 0; i < fft_size; i++)	// multiply by window / N
				samples[i] *= fft_window[i] / size;
		}
		else {
			for (i = 0; i < fft_size; i++)	// divide by N
				samples[i] /= size;
		}
	}
	else {
		if (window) {
			for (i = 0; i < fft_size; i++)	// multiply by window
				samples[i] *= fft_window[i];
	   }
		fftw_execute(planF);		// Calculate FFT
	}
	pyseq = PyList_New(fft_size);
	j = (size - 1) / 2;		// zero frequency in input
	for (i = 0; i < fft_size; i++) {
		pycx.real = creal(samples[i]);
		pycx.imag = cimag(samples[i]);
		PyList_SetItem(pyseq, j, PyComplex_FromCComplex(pycx));
		if (++j >= size)
			j = 0;
	}
	return pyseq;
}

static PyObject * dft(PyObject * self, PyObject * args)
{
	PyObject * tuple2;
	int window;

	window = 0;
	if (!PyArg_ParseTuple (args, "O|i", &tuple2, &window))
		return NULL;
	return Xdft(tuple2, 0, window);
}

static PyObject * is_key_down(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, ""))
		return NULL;
	return PyInt_FromLong(quisk_is_key_down());
}

static PyObject * idft(PyObject * self, PyObject * args)
{
	PyObject * tuple2;
	int window;

	window = 0;
	if (!PyArg_ParseTuple (args, "O|i", &tuple2, &window))
		return NULL;
	return Xdft(tuple2, 1, window);
}

static PyObject * record_app(PyObject * self, PyObject * args)
{  // Record the Python object for the application instance, malloc space for fft's.
	int i, j, rate;
	fftw_complex * pt;

	if (!PyArg_ParseTuple (args, "OOiiiil", &pyApp, &quisk_pyConfig, &data_width,
		&fft_size, &average_count, &rate, &quisk_mainwin_handle))
		return NULL;

	Py_INCREF(quisk_pyConfig);

	rx_udp_clock = QuiskGetConfigDouble("rx_udp_clock", 122.88e6);
	graph_refresh = QuiskGetConfigInt("graph_refresh", 7);
	use_rx_udp = QuiskGetConfigInt("use_rx_udp", 0);
	quisk_sound_state.sample_rate = rate;
	fft_sample_rate = rate;
	is_little_endian = 1;	// Test machine byte order
	if (*(char *)&is_little_endian == 1)
		is_little_endian = 1;
	else
		is_little_endian = 0;
	strncpy (quisk_sound_state.err_msg, CLOSED_TEXT, QUISK_SC_SIZE);
	// Initialize space for the FFTs
	for (i = 0; i < FFT_ARRAY_SIZE; i++) {
		fft_data_array[i].filled = 0;
		fft_data_array[i].index = 0;
		fft_data_array[i].block = 0;
		pt = fft_data_array[i].samples = (fftw_complex *) fftw_malloc(sizeof(fftw_complex) * fft_size);
		fft_data_array[i].plan = fftw_plan_dft_1d(fft_size, pt, pt, FFTW_FORWARD, FFTW_MEASURE);
	}
	// Create space for the fft average and window
	if (fft_window)
		free(fft_window);
	fft_window = (double *) malloc(sizeof(double) * fft_size);
	for (i = 0, j = -fft_size / 2; i < fft_size; i++, j++) {
		if (0)	// Hamming
			fft_window[i] = 0.54 + 0.46 * cos(2. * M_PI * j / fft_size);
		else	// Hanning
			fft_window[i] = 0.5 + 0.5 * cos(2. * M_PI * j / fft_size);
	}
	if (current_graph)
		free(current_graph);
	current_graph = (double *) malloc(sizeof(double) * data_width);
	measure_freq(NULL, 0, 0);
	dAutoNotch(NULL, 0, 0, 0);
	quisk_process_decimate(NULL, 0, 0);
	quisk_process_demodulate(NULL, NULL, 0, 0);
	//calc_audio_graph(NULL, 0);
#if DEBUG_IO
	QuiskPrintTime(NULL, 0);
#endif
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * record_graph(PyObject * self, PyObject * args)
{  /* record the Python object for the application instance */
	if (!PyArg_ParseTuple (args, "iid", &graphX, &graphY, &graphScale))
		return NULL;
	graphScale *= 2;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * test_1(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, ""))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * test_2(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, ""))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * test_3(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, ""))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyObject * set_fdx(PyObject * self, PyObject * args)
{
	if (!PyArg_ParseTuple (args, "i", &isFDX))
		return NULL;
	Py_INCREF (Py_None);
	return Py_None;
}

static PyMethodDef QuiskMethods[] = {
	{"add_tone", add_tone, METH_VARARGS, "Add a test tone to the data."},
	{"dft", dft, METH_VARARGS, "Calculate the discrete Fourier transform."},
	{"idft", idft, METH_VARARGS, "Calculate the inverse discrete Fourier transform."},
	{"is_key_down", is_key_down, METH_VARARGS, "Check whether the key is down; return 0 or 1."},
	{"get_state", get_state, METH_VARARGS, "Return a count of read and write errors."},
	{"get_graph", get_graph, METH_VARARGS, "Return a tuple of graph data."},
	{"get_filter", get_filter, METH_VARARGS, "Return the frequency response of the receive filter."},
	{"get_filter_rate", get_filter_rate, METH_VARARGS, "Return the sample rate used for the filters."},
	{"get_tx_filter", quisk_get_tx_filter, METH_VARARGS, "Return the frequency response of the transmit filter."},
	{"get_audio_graph", get_audio_graph, METH_VARARGS, "Return a tuple of the audio graph data."},
	{"measure_frequency", measure_frequency, METH_VARARGS, "Set the method, return the measured frequency."},
	{"measure_audio", measure_audio, METH_VARARGS, "Set the method, return the measured audio voltage."},
	{"get_overrange", get_overrange, METH_VARARGS, "Return the count of overrange (clip) for the ADC."},
	{"get_smeter", get_smeter, METH_VARARGS, "Return the S meter reading."},
	{"invert_spectrum", invert_spectrum, METH_VARARGS, "Invert the input RF spectrum"},
	{"record_app", record_app, METH_VARARGS, "Save the App instance."},
	{"record_graph", record_graph, METH_VARARGS, "Record graph parameters."},
	{"set_ampl_phase", quisk_set_ampl_phase, METH_VARARGS, "Set the sound card amplitude and phase corrections."},
	{"set_udp_tx_correct", quisk_set_udp_tx_correct, METH_VARARGS, "Set the UDP transmit corrections."},
	{"set_agc", set_agc, METH_VARARGS, "Set the AGC parameters."},
	{"set_squelch", set_squelch, METH_VARARGS, "Set the squelch parameter."},
	{"get_squelch", get_squelch, METH_VARARGS, "Get the squelch state, 0 or 1."},
	{"set_file_record", quisk_set_file_record, METH_VARARGS, "Set the state and names of the recording files."},
	{"set_filters", set_filters, METH_VARARGS, "Set the receive audio I and Q channel filters."},
	{"set_auto_notch", set_auto_notch, METH_VARARGS, "Set the auto notch on or off."},
	{"set_kill_audio", set_kill_audio, METH_VARARGS, "Replace radio sound with silence."},
	{"set_noise_blanker", set_noise_blanker, METH_VARARGS, "Set the noise blanker level."},
	{"set_record_state", set_record_state, METH_VARARGS, "Set the temp buffer record and playback state."},
	{"set_rx_mode", set_rx_mode, METH_VARARGS, "Set the receive mode: CWL, USB, AM, etc."},
	{"set_mic_out_volume", set_mic_out_volume, METH_VARARGS, "Set the level of the mic output for SoftRock transmit"},
	{"set_spot_level", set_spot_level, METH_VARARGS, "Set the spot level, or zero for no spot"},
	{"set_sidetone", set_sidetone, METH_VARARGS, "Set the sidetone volume and frequency."},
	{"set_transmit_mode", set_transmit_mode, METH_VARARGS, "Change the radio to transmit mode independent of key_down."},
	{"set_volume", set_volume, METH_VARARGS, "Set the audio output volume."},
	{"set_tx_audio", (PyCFunction)quisk_set_tx_audio, METH_VARARGS|METH_KEYWORDS, "Set the transmit audio parameters."},
	{"is_vox", quisk_is_vox, METH_VARARGS, "return the VOX state zero or one."},
	{"set_split_rxtx", set_split_rxtx, METH_VARARGS, "Set split for rx/tx."},
	{"set_tune", set_tune, METH_VARARGS, "Set the tuning frequency."},
	{"test_1", test_1, METH_VARARGS, "Test 1 function."},
	{"test_2", test_2, METH_VARARGS, "Test 2 function."},
	{"test_3", test_3, METH_VARARGS, "Test 3 function."},
	{"set_fdx", set_fdx, METH_VARARGS, "Set full duplex mode; ignore the key status."},
	{"sound_devices", quisk_sound_devices, METH_VARARGS, "Return a list of available sound device names."},
	{"pa_sound_devices", quisk_pa_sound_devices, METH_VARARGS, "Return a list of available PulseAudio sound device names."},
	{"sound_errors", quisk_sound_errors, METH_VARARGS, "Return a list of text strings with sound devices and error counts"},
	{"open_sound", open_sound, METH_VARARGS, "Open the the soundcard device."},
	{"close_sound", close_sound, METH_VARARGS, "Stop the soundcard and release resources."},
	{"capt_channels", quisk_capt_channels, METH_VARARGS, "Set the I and Q capture channel numbers"},
	{"play_channels", quisk_play_channels, METH_VARARGS, "Set the I and Q playback channel numbers"},
	{"micplay_channels", quisk_micplay_channels, METH_VARARGS, "Set the I and Q microphone playback channel numbers"},
	{"change_scan", change_scan, METH_VARARGS, "Change to a new FFT rate and multiplier"},
	{"change_rate", change_rate, METH_VARARGS, "Change to a new sample rate"},
	{"change_rates", change_rates, METH_VARARGS, "Change to multiple new sample rates"},
	{"read_sound", read_sound, METH_VARARGS, "Read from the soundcard."},
	{"start_sound", start_sound, METH_VARARGS, "Start the soundcard."},
	{"mixer_set", mixer_set, METH_VARARGS, "Set microphone mixer parameters such as volume."},
	{"open_key", open_key, METH_VARARGS, "Open access to the state of the key (CW or PTT)."},
	{"open_rx_udp", open_rx_udp, METH_VARARGS, "Open a UDP port for capture."},
	{"close_rx_udp", close_rx_udp, METH_VARARGS, "Close the UDP port used for capture."},
	{"set_key_down", set_key_down, METH_VARARGS, "Change the key up/down state for method \"\""},
	{NULL, NULL, 0, NULL}		/* Sentinel */
};

PyMODINIT_FUNC init_quisk (void)
{
	PyObject * m;
	PyObject * c_api_object;
	static void * Quisk_API[] = QUISK_API_INIT;

	m = Py_InitModule ("_quisk", QuiskMethods);
	if (m == NULL) {
		printf("Py_InitModule of _quisk failed!\n");
		return;
	}

	QuiskError = PyErr_NewException ("quisk.error", NULL, NULL);
	Py_INCREF (QuiskError);
	PyModule_AddObject (m, "error", QuiskError);

#if ( (PY_VERSION_HEX <  0x02070000) || ((PY_VERSION_HEX >= 0x03000000) && (PY_VERSION_HEX <  0x03010000)) )
// Old Python interface using CObject
       /* Create CObjects for handing _quisk symbols to C extensions in other Python modules. */
       c_api_object = PyCObject_FromVoidPtr(Quisk_API, NULL);
       if (c_api_object != NULL)
         PyModule_AddObject(m, "QUISK_C_API", c_api_object);
#else
// New Python interface using Capsule
       /* Create Capsules for handing _quisk symbols to C extensions in other Python modules. */
       c_api_object = PyCapsule_New(Quisk_API, "_quisk.QUISK_C_API", NULL);
       if (c_api_object != NULL)
         PyModule_AddObject(m, "QUISK_C_API", c_api_object);
#endif
}
