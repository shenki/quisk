/*
 * sound_pulseaudio.c is part of Quisk, and is Copyright the following
 * authors:
 * 
 * Philip G. Lee <rocketman768@gmail.com>, 2014
 * Jim Ahlstrom, N2ADR, October, 2014
 *
 * Quisk is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.

 * Quisk is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.

 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

#include <Python.h>
#include <stdio.h>
#include <complex.h>
#include "quisk.h"

#include <pulse/pulseaudio.h>
#include <pulse/simple.h>
#include <pulse/error.h>

// Current sound status
extern struct sound_conf quisk_sound_state;

// Buffer for float32 samples from sound
static float fbuffer[SAMP_BUFFER_SIZE];

/*!
 * \brief Read sound samples from PulseAudio.
 * 
 * Samples are converted to 32 bits with a range of +/- CLIP32 and placed into
 * cSamples.
 * 
 * \param dev Input. The device from which to read audio.
 * \param cSamples Output. The output samples.
 * 
 * \returns the number of samples placed into \c cSamples
 */
int quisk_read_pulseaudio(
   struct sound_dev* dev,
   complex double * cSamples
)
{
   pa_simple* padev = (pa_simple*)(dev->handle);
   int error = 0;
   int ret = 0;
   int i = 0;
   int nSamples = 0;
   int num_channels = dev->num_channels;
   // A frame is a sample from each channel
   int read_frames = dev->read_frames;
   // If 0, we are expected to do non-blocking read, but ignore for now and
   // keep doing the blocking read.
   if( read_frames == 0 )
      read_frames = (int)(quisk_sound_state.data_poll_usec * 1e-6 * dev->sample_rate + 0.5);
   int read_bytes = read_frames * num_channels * sizeof(float);
   float fi=0.f, fq=0.f;
   complex double c;
   
   // Read and check for errors.
   ret = pa_simple_read( padev, fbuffer, read_bytes, &error );
   if( ret < 0 )
   {
      dev->dev_error++;
      
#if DEBUG_IO
      fprintf(
         stderr,
         __FILE__": quisk_read_pulseaudio() failed %s\n",
         pa_strerror(error)
      );
#endif
      
      return 0;
   }
   
   // Convert sampled data to complex data
   for( i = 0, nSamples = 0; nSamples < read_frames; i += num_channels, ++nSamples )
   {
      fi = fbuffer[i + dev->channel_I];
      fq = fbuffer[i + dev->channel_Q];
      if (fi >=  1.0 || fi <= -1.0)
         dev->overrange++;
      if (fq >=  1.0 || fq <= -1.0)
         dev->overrange++;
      cSamples[nSamples] = (fi + I * fq) * CLIP32;
   }
   
   // DC removal; R.G. Lyons page 553
   for (i = 0; i < nSamples; i++)
   {
      c = cSamples[i] + dev->dc_remove * 0.95;
      cSamples[i] = c - dev->dc_remove;
      dev->dc_remove = c;
   }
   
   return nSamples;
}

/*!
 * \brief Play the samples; write them to PulseAudio.
 * 
 * \param playdev Input. Device to which to play the samples.
 * \param nSamples Input. Number of samples to play.
 * \param cSamples Input. Sample buffer to play from.
 * \param report_latency Input. 1 to update \c quisk_sound_state.latencyPlay, 0 otherwise.
 * \param volume Input. Ratio in [0,1] by which to scale the played samples.
 */
void quisk_play_pulseaudio(
   struct sound_dev* playdev,
   int nSamples,
   complex double * cSamples,
   int report_latency,
   double volume
)
{
   pa_simple* padev = (pa_simple*)(playdev->handle);
   pa_usec_t latency = 0;
   size_t nBytes = nSamples * playdev->num_channels * sizeof(float);
   int error = 0;
   int ret = 0;
   float fi=0.f, fq=0.f;
   int i=0, n=0;

   if( !padev || nSamples <= 0)
      return;
   
   // Convert from complex data to framebuffer
   for(i = 0, n = 0; n < nSamples; i += playdev->num_channels, ++n)
   {
      fi = volume * creal(cSamples[n]);
      fq = volume * cimag(cSamples[n]);
      fbuffer[i + playdev->channel_I] = fi / CLIP32;
      fbuffer[i + playdev->channel_Q] = fq / CLIP32;
   }
   
   // Report the latency, if requested.
   if( report_latency )
   {
      latency = pa_simple_get_latency( padev, &error );
      if( latency == (pa_usec_t)(-1) )
      {
#if DEBUG_IO
         fprintf(
            stderr,
            __FILE__": quisk_play_pulseaudio() failed %s\n",
            pa_strerror(error)
         );
#endif
         
         playdev->dev_error++;
      }
      else
      {
         // Samples left in play buffer
         //quisk_sound_state.latencyPlay = (latency * quisk_sound_state.playback_rate) / 1e6;
         quisk_sound_state.latencyPlay = latency;
      }
   }
   
   // Write data and check for errors
   ret = pa_simple_write( padev, fbuffer, nBytes, &error );
   if( ret < 0 )
   {
#if DEBUG_IO
      fprintf(
         stderr,
         __FILE__": quisk_play_pulseaudio() failed %s\n",
         pa_strerror(error)
      );
#endif
      
      quisk_sound_state.write_error++;
      playdev->dev_error++;
   }
}

/*!
 * \brief Open PulseAudio device.
 * 
 * \param dev Input/Output. Device to initialize. \c dev->sample_rate and
 *        \c dev->num_channels must be properly set before calling. On return,
 *        \c dev->handle holds a \c pa_simple pointer, and \c dev->driver is
 *        set to \c DEV_DRIVER_PULSEAUDIO.
 * \param direction Input. Usually either \c PA_STREAM_PLAYBACK or
 *        \c PA_STREAM_RECORD
 * \param stream_description Input. Can be null for no description, but please
 *        provide a unique one
 * 
 * \return zero for no error, and nonzero result of \c pa_simple_new() for
 *         error
 */
static int quisk_open_pulseaudio(
   struct sound_dev* dev,
   pa_stream_direction_t direction,
   const char* stream_description
)
{
   pa_sample_spec ss;
   //pa_buffer_attr ba;
   int error = 0;
   const char * dev_name;	// modified N2ADR
   
   // Device without name: sad.
   if( ! dev->name[0] )
      return 0;
   
   // Construct sample specification
   ss.format = PA_SAMPLE_FLOAT32LE;
   ss.rate = dev->sample_rate;
   ss.channels = dev->num_channels;
   
   // Construct buffer attributes. Letting PulseAudio do it's thing seems to be
   // the best thing to do here.
   //if( direction == PA_STREAM_PLAYBACK )
   //   ba.maxlength = (sizeof(float)*dev->num_channels) * dev->sample_rate * quisk_sound_state.latency_millisecs / 1e3;
   
   if (dev->name[5] == ':')
      dev_name = dev->name + 6;		// the device name is given; "pulse:alsa_input.pci-0000_00_1b.0.analog-stereo"
   else
      dev_name = NULL;				// the device name is "pulse" for the default device
   pa_simple* s;
   s = pa_simple_new(
      NULL, // Default server
      "Quisk", // Application name
      direction,
      dev_name, // Device name
      stream_description,
      &ss, // Sample format
      NULL, // Default channel map
      //&ba, // Buffering attributes
      NULL,
      &error
   );
   
   if( error )
   {
		snprintf (quisk_sound_state.err_msg, QUISK_SC_SIZE, "quisk_open_pulseaudio failed %s %s",
			pa_strerror(error), dev->name);
		dev->handle = NULL;
		dev->driver = DEV_DRIVER_NONE;
		return error;
   }

   dev->handle = (void*)(s);
   dev->driver = DEV_DRIVER_PULSEAUDIO;
   return 0;
}

/*!
 * \brief Search for and open PulseAudio devices.
 * 
 * \param pCapture Input/Output. Array of capture devices to search through.
 *        If a device has its \c sound_dev.driver field set to
 *        \c DEV_DRIVER_PULSEAUDIO, it will be opened for recording.
 * \param pPlayback Input/Output. Array of playback devices to search through.
 *        If a device has its \c sound_dev.driver field set to
 *        \c DEV_DRIVER_PULSEAUDIO, it will be opened for recording.
 */
void quisk_start_sound_pulseaudio(
   struct sound_dev** pCapture,
   struct sound_dev** pPlayback
)
{
   struct sound_dev* dev;
   
   // Open all capture devices
   while(1)
   {
      dev = *pCapture++;
      // Done if null
      if( !dev )
         break;
      // Continue if it's not our device
      else if( dev->driver != DEV_DRIVER_PULSEAUDIO )
         continue;
      
      if( quisk_open_pulseaudio(dev, PA_STREAM_RECORD, dev->stream_description) )
         return;
   }
   
   // Open all playback devices
   while(1)
   {
      dev = *pPlayback++;
      // Done if null
      if( !dev )
         break;
      // Continue if it's not our device
      else if( dev->driver != DEV_DRIVER_PULSEAUDIO )
         continue;
      
      if( quisk_open_pulseaudio(dev, PA_STREAM_PLAYBACK, dev->stream_description) )
         return;
   }
}

/*!
 * \brief Search for and close PulseAudio devices.
 * 
 * \param pCapture Input/Output. Array of capture devices to search through.
 *        If a device has its \c sound_dev.driver field set to
 *        \c DEV_DRIVER_PULSEAUDIO, it will be closed.
 * \param pPlayback Input/Output. Array of capture devices to search through.
 *        If a device has its \c sound_dev.driver field set to
 *        \c DEV_DRIVER_PULSEAUDIO, it will be closed.
 */
void quisk_close_sound_pulseaudio(
   struct sound_dev** pCapture,
   struct sound_dev** pPlayback
)
{
   struct sound_dev* dev;
   int ret;
   int error;
   
   while(1)
   {
      dev = *pCapture++;
      
      // Done if null
      if( !dev )
         break;
      // Continue if it's not our device
      else if( dev->driver != DEV_DRIVER_PULSEAUDIO )
         continue;
      
      ret = pa_simple_drain( (pa_simple*)(dev->handle), &error );
      if( ret < 0 )
      {
#if DEBUG_IO
         fprintf(
            stderr,
            __FILE__": quisk_close_sound_pulseaudio() failed %s\n",
            pa_strerror(error)
         );
#endif
      }
      
      pa_simple_free( (pa_simple*)(dev->handle) );
      dev->handle = NULL;
      dev->driver = DEV_DRIVER_NONE;
   }
   
   while(1)
   {
      dev = *pPlayback++;
      if( !dev )
         break;
      
      // Done if null
      if( !dev )
         break;
      // Continue if it's not our device
      else if( dev->driver != DEV_DRIVER_PULSEAUDIO )
         continue;
      
      ret = pa_simple_flush( (pa_simple*)(dev->handle), &error );
      if( ret < 0 )
      {
#if DEBUG_IO
         fprintf(
            stderr,
            __FILE__": quisk_close_sound_pulseaudio() failed %s\n",
            pa_strerror(error)
         );
#endif
      }
      
      pa_simple_free( (pa_simple*)(dev->handle) );
      dev->handle = NULL;
      dev->driver = DEV_DRIVER_NONE;
   }
}

// Additional bugs added by N2ADR below this point.
// Code for quisk_pa_sound_devices is based on code by Igor Brezac and Eric Connell, and Jan Newmarch.

// This callback gets called when our context changes state.  We really only
// care about when it's ready or if it has failed.
static void pa_state_cb(pa_context *c, void *userdata) {
	pa_context_state_t ctx_state;
	int *main_state = userdata;

	ctx_state = pa_context_get_state(c);
	switch  (ctx_state) {
		// There are just here for reference
		case PA_CONTEXT_UNCONNECTED:
		case PA_CONTEXT_CONNECTING:
		case PA_CONTEXT_AUTHORIZING:
		case PA_CONTEXT_SETTING_NAME:
		default:
			break;
		case PA_CONTEXT_FAILED:
		case PA_CONTEXT_TERMINATED:
			*main_state = 9;
			break;
		case PA_CONTEXT_READY:
			*main_state = 1;
			break;
	}
}

static void source_sink(const char * name, const char * descr, pa_proplist * props, PyObject * pylist) {
	const char * value;
	char buf300[300];
	PyObject * pytup;

	pytup = PyTuple_New(3);
	PyList_Append(pylist, pytup);
	PyTuple_SET_ITEM(pytup, 0, PyString_FromString(name));
	PyTuple_SET_ITEM(pytup, 1, PyString_FromString(descr));
	value = pa_proplist_gets(props, "device.api");
	if (value && ! strcmp(value, "alsa")) {
		snprintf(buf300, 300, "%s %s (hw:%s,%s)", pa_proplist_gets(props, "alsa.card_name"), pa_proplist_gets(props, "alsa.name"),
			 pa_proplist_gets(props, "alsa.card"), pa_proplist_gets(props, "alsa.device"));

		PyTuple_SET_ITEM(pytup, 2, PyString_FromString(buf300));
	}
	else {
		PyTuple_SET_ITEM(pytup, 2, PyString_FromString(""));
	}
}

// pa_mainloop will call this function when it's ready to tell us about a sink.
static void pa_sinklist_cb(pa_context *c, const pa_sink_info *l, int eol, void *userdata) {
    if (eol > 0)	// If eol is set to a positive number, you're at the end of the list
		return;
    source_sink(l->name, l->description, l->proplist, (PyObject *)userdata);
}

static void pa_sourcelist_cb(pa_context *c, const pa_source_info *l, int eol, void *userdata) {
    if (eol > 0)
		return;
    source_sink(l->name, l->description, l->proplist, (PyObject *)userdata);
}

PyObject * quisk_pa_sound_devices(PyObject * self, PyObject * args)
{	// Return a list of PulseAudio device names [pycapt, pyplay]
	PyObject * pylist, * pycapt, * pyplay;
	pa_mainloop *pa_ml;
	pa_mainloop_api *pa_mlapi;
	pa_operation *pa_op=NULL;
	pa_context *pa_ctx;
	int state = 0;

	if (!PyArg_ParseTuple (args, ""))
		return NULL;
	// Each pycapt and pyplay is (dev name, description, alsa name)
	pylist = PyList_New(0);		// list [pycapt, pyplay]
	pycapt = PyList_New(0);		// list of capture devices
	pyplay = PyList_New(0);		// list of play devices
	PyList_Append(pylist, pycapt);
	PyList_Append(pylist, pyplay);

	// Create a mainloop API and connection to the default server
	pa_ml = pa_mainloop_new();
	pa_mlapi = pa_mainloop_get_api(pa_ml);
	pa_ctx = pa_context_new(pa_mlapi, "DeviceNames");

	// This function connects to the pulse server
	pa_context_connect(pa_ctx, NULL, 0, NULL);

	// This function defines a callback so the server will tell us it's state.
	pa_context_set_state_callback(pa_ctx, pa_state_cb, &state);

	// Now we'll enter into an infinite loop until we get the data we receive or if there's an error
	while (state < 10) {
		switch (state) {
		case 0:	// We can't do anything until PA is ready
			pa_mainloop_iterate(pa_ml, 1, NULL);
			break;
		case 1:
			// This sends an operation to the server.  pa_sinklist_info is
			// our callback function and a pointer to our devicelist will
			// be passed to the callback.
			pa_op = pa_context_get_sink_info_list(pa_ctx, pa_sinklist_cb, pyplay);
			// Update state for next iteration through the loop
			state++;
			pa_mainloop_iterate(pa_ml, 1, NULL);
			break;
		case 2:
			// Now we wait for our operation to complete.  When it's
			// complete our pa_output_devicelist is filled out, and we move
			// along to the next state
			if (pa_operation_get_state(pa_op) == PA_OPERATION_DONE) {
				pa_operation_unref(pa_op);
				// Now we perform another operation to get the source
				// (input device) list just like before.
				pa_op = pa_context_get_source_info_list(pa_ctx, pa_sourcelist_cb, pycapt);
				// Update the state so we know what to do next
				state++;
			}
			pa_mainloop_iterate(pa_ml, 1, NULL);
			break;
		case 3:
			if (pa_operation_get_state(pa_op) == PA_OPERATION_DONE) {
				pa_operation_unref(pa_op);
				state = 9;
			}
			else
				pa_mainloop_iterate(pa_ml, 1, NULL);
			break;
		case 9:				// Now we're done, clean up and disconnect and return
			pa_context_disconnect(pa_ctx);
			pa_context_unref(pa_ctx);
			pa_mainloop_free(pa_ml);
			state = 99;
			break;
		}
	}
	return pylist;
}
