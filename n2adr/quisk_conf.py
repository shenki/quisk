# This is the config file from my shack, which controls various hardware.
# The files to control my 2010 transceiver and for the improved version HiQSDR
# are in the package directory HiQSDR.

#from quisk_conf_defaults import *
import sys
from n2adr import quisk_hardware
from n2adr import quisk_widgets

if sys.platform == "win32":
  n2adr_sound_pc_capt = 'Line In (Realtek High Definition Audio)'
  n2adr_sound_pc_play = 'Speakers (Realtek High Definition Audio)'
  n2adr_sound_usb_play = 'Primary'
  n2adr_sound_usb_mic = 'Line In (USB Multi-Channel'
  latency_millisecs = 150
  data_poll_usec = 20000
  favorites_file_path = "C:/pub/quisk_favorites.txt"
elif 0:		# portaudio devices
  name_of_sound_play = 'portaudio:CODEC USB'
  microphone_name = "portaudio:AK5370"
  latency_millisecs = 150
  data_poll_usec = 5000
  favorites_file_path = "/home/jim/pub/quisk_favorites.txt"
elif 0:		# pulseaudio devices
  n2adr_sound_pc_capt = ''
  n2adr_sound_pc_play = 'pulse'
  n2adr_sound_usb_play = 'pulse:alsa_output.usb'
  n2adr_sound_usb_mic = ''
  latency_millisecs = 150
  data_poll_usec = 5000
  favorites_file_path = "/home/jim/pub/quisk_favorites.txt"
else:		# alsa devices
  n2adr_sound_pc_capt = 'alsa:ALC888-VD'
  n2adr_sound_pc_play = 'alsa:ALC888-VD'
  n2adr_sound_usb_play = 'alsa:USB Sound Device'
  n2adr_sound_usb_mic = 'alsa:USB Sound Device'
  latency_millisecs = 150
  data_poll_usec = 5000
  favorites_file_path = "/home/jim/pub/quisk_favorites.txt"

name_of_sound_capt = ""
name_of_sound_play = n2adr_sound_usb_play
microphone_name = n2adr_sound_usb_mic

mic_sample_rate = 48000
playback_rate = 48000
agc_off_gain = 80
tx_level = {None:100, '60':100, '40':110, '30':100, '20':90, '15':150, '12':170, '10':130}

default_screen = 'WFall'
waterfall_y_scale = 80
waterfall_y_zero  = 40
waterfall_graph_y_scale = 40
waterfall_graph_y_zero = 90
waterfall_graph_size = 160

station_display_lines = 1
# DX cluster telent login data, thanks to DJ4CM.
dxClHost = ''
#dxClHost = 'dxc.w8wts.net'
dxClPort = 7373
user_call_sign = 'n2adr'

add_imd_button = 1
add_fdx_button = 1

use_rx_udp = 1				# Get ADC samples from UDP
rx_udp_ip = "192.168.1.196"		# Sample source IP address
rx_udp_port = 0xBC77			# Sample source UDP port
rx_udp_clock = 122880000  		# ADC sample rate in Hertz
rx_udp_decimation = 8 * 8 * 8		# Decimation from clock to UDP sample rate
sample_rate = int(float(rx_udp_clock) / rx_udp_decimation + 0.5)	# Don't change this
data_poll_usec = 10000
display_fraction = 1.00
tx_ip = "192.168.1.196"
tx_audio_port = 0xBC79
mic_out_volume = 0.8

mixer_settings = [
    #(microphone_name, 2, 0.80),	# numid of microphone volume control, volume 0.0 to 1.0;
    #(microphone_name, 1, 1.0)		# numid of capture on/off control, turn on with 1.0;
    #(microphone_name, 8, 1.00),	# numid of microphone volume control, volume 0.0 to 1.0;
    #(microphone_name, 7, 1.0)		# numid of capture on/off control, turn on with 1.0;
    (microphone_name, 14, 1),		# capture from line
    (microphone_name,  9, 1),		# line capture ON
    (microphone_name, 10, 0.65),	# line capture level
  ]
