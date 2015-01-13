#! /usr/bin/python

# All QUISK software is Copyright (C) 2006-2011 by James C. Ahlstrom.
# This free software is licensed for use under the GNU General Public
# License (GPL), see http://www.opensource.org.
# Note that there is NO WARRANTY AT ALL.  USE AT YOUR OWN RISK!!

"""The main program for Quisk, a software defined radio.

Usage:  python quisk.py [-c | --config config_file_path]
This can also be installed as a package and run as quisk.main().
"""

from __future__ import print_function

# Change to the directory of quisk.py.  This is necessary to import Quisk packages
# and to load other extension modules that link against _quisk.so.  It also helps to
# find ./__init__.py and ./help.html.
import sys, os
os.chdir(os.path.normpath(os.path.dirname(__file__)))
if sys.path[0] != "'.'":		# Make sure the current working directory is on path
  sys.path.insert(0, '.')

import wxversion				# Thanks to Mario, DH5YM
wxversion.ensureMinimal('2.8')

import wx, wx.html, wx.lib.buttons, wx.lib.stattext, wx.lib.colourdb, wx.grid, wx.richtext
import math, cmath, time, traceback, string
import threading, pickle, webbrowser
if sys.version_info[0] == 3:	# Python3
  from xmlrpc.client import ServerProxy
else:				# Python version 2.x
  from xmlrpclib import ServerProxy
import _quisk as QS
from types import *
from quisk_widgets import *
from filters import Filters
import dxcluster

# Fldigi XML-RPC control opens a local socket.  If socket.setdefaulttimeout() is not
# called, the timeout on Linux is zero (1 msec) and on Windows is 2 seconds.  So we
# call it to insure consistent behavior.
import socket
socket.setdefaulttimeout(0.005)

# Command line parsing: be able to specify the config file.
from optparse import OptionParser
parser = OptionParser()
parser.add_option('-c', '--config', dest='config_file_path',
		help='Specify the configuration file path')
parser.add_option('', '--config2', dest='config_file_path2', default='',
		help='Specify a second configuration file to read after the first')
argv_options = parser.parse_args()[0]
ConfigPath = argv_options.config_file_path	# Get config file path
ConfigPath2 = argv_options.config_file_path2
if not ConfigPath:	# Use default path
  if sys.platform == 'win32':
    path = os.getenv('HOMEDRIVE', '') + os.getenv('HOMEPATH', '')
    for dir in ("Documents", "My Documents", "Eigene Dateien", "Documenti", "Mine Dokumenter"):
      ConfigPath = os.path.join(path, dir)
      if os.path.isdir(ConfigPath):
        break
    else:
      ConfigPath = os.path.join(path, "My Documents")
    ConfigPath = os.path.join(ConfigPath, "quisk_conf.py")
    if not os.path.isfile(ConfigPath):	# See if the user has a config file
      try:
        import shutil	# Try to create an initial default config file
        shutil.copyfile('quisk_conf_win.py', ConfigPath)
      except:
        pass
  else:
    ConfigPath = os.path.expanduser('~/.quisk_conf.py')

# These FFT sizes have multiple small factors, and are prefered for efficiency:
fftPreferedSizes = (416, 448, 480, 512, 576, 640, 672, 704, 768, 800, 832,
864, 896, 960, 1024, 1056, 1120, 1152, 1248, 1280, 1344, 1408, 1440, 1536,
1568, 1600, 1664, 1728, 1760, 1792, 1920, 2016, 2048, 2080, 2112, 2240, 2304,
2400, 2464, 2496, 2560, 2592, 2688, 2816, 2880, 2912)

def round(x):	# round float to nearest integer
  if x >= 0:
    return int(x + 0.5)
  else:
    return - int(-x + 0.5)
    
def str2freq (freq):
  if '.' in freq:
    freq = int(float(freq) * 1E6 + 0.1)
  else:
    freq = int(freq)
  return freq    

class Timer:
  """Debug: measure and print times every ptime seconds.

  Call with msg == '' to start timer, then with a msg to record the time.
  """
  def __init__(self, ptime = 1.0):
    self.ptime = ptime		# frequency to print in seconds
    self.time0 = 0			# time zero; measure from this time
    self.time_print = 0		# last time data was printed
    self.timers = {}		# one timer for each msg
    self.names = []			# ordered list of msg
    self.heading = 1		# print heading on first use
  def __call__(self, msg):
    tm = time.time()
    if msg:
      if not self.time0:		# Not recording data
        return
      if msg in self.timers:
        count, average, highest = self.timers[msg]
      else:
        self.names.append(msg)
        count = 0
        average = highest = 0.0
      count += 1
      delta = tm - self.time0
      average += delta
      if highest < delta:
        highest = delta
      self.timers[msg] = (count, average, highest)
      if tm - self.time_print > self.ptime:	# time to print results
        self.time0 = 0		# end data recording, wait for reset
        self.time_print = tm
        if self.heading:
          self.heading = 0
          print ("count, msg, avg, max (msec)")
        print("%4d" % count, end=' ')
        for msg in self.names:		# keep names in order
          count, average, highest = self.timers[msg]
          if not count:
            continue
          average /= count
          print("  %s  %7.3f  %7.3f" % (msg, average * 1e3, highest * 1e3), end=' ')
          self.timers[msg] = (0, 0.0, 0.0)
        print()
    else:	# reset the time to zero
      self.time0 = tm		# Start timer
      if not self.time_print:
        self.time_print = tm

## T = Timer()		# Make a timer instance

class HamlibHandler:
  """This class is created for each connection to the server.  It services requests from each client"""
  SingleLetters = {		# convert single-letter commands to long commands
    '_':'info',
    'f':'freq',
    'i':'split_freq',
    'm':'mode',
    's':'split_vfo',
    't':'ptt',
    'v':'vfo',
    }
# I don't understand the need for dump_state, nor what it means.
# A possible response to the "dump_state" request:
  dump1 = """ 2
2
2
150000.000000 1500000000.000000 0x1ff -1 -1 0x10000003 0x3
0 0 0 0 0 0 0
0 0 0 0 0 0 0
0x1ff 1
0x1ff 0
0 0
0x1e 2400
0x2 500
0x1 8000
0x1 2400
0x20 15000
0x20 8000
0x40 230000
0 0
9990
9990
10000
0
10 
10 20 30 
0x3effffff
0x3effffff
0x7fffffff
0x7fffffff
0x7fffffff
0x7fffffff
"""

# Another possible response to the "dump_state" request:
  dump2 = """ 0
2
2
150000.000000 30000000.000000  0x900af -1 -1 0x10 000003 0x3
0 0 0 0 0 0 0
150000.000000 30000000.000000  0x900af -1 -1 0x10 000003 0x3
0 0 0 0 0 0 0
0 0
0 0
0
0
0
0


0x0
0x0
0x0
0x0
0x0
0
"""
  def __init__(self, app, sock, address):
    self.app = app		# Reference back to the "hardware"
    self.sock = sock
    sock.settimeout(0.0)
    self.address = address
    self.received = ''
    h = self.Handlers = {}
    h[''] = self.ErrProtocol
    h['dump_state']	= self.DumpState
    h['get_freq']	= self.GetFreq
    h['set_freq']	= self.SetFreq
    h['get_info']	= self.GetInfo
    h['get_mode']	= self.GetMode
    h['set_mode']	= self.SetMode
    h['get_vfo']	= self.GetVfo
    h['get_ptt']	= self.GetPtt
    h['set_ptt']	= self.SetPtt
    h['get_split_freq']	= self.GetSplitFreq
    h['set_split_freq']	= self.SetSplitFreq
    h['get_split_vfo']	= self.GetSplitVfo
    h['set_split_vfo']	= self.SetSplitVfo
  def Send(self, text):
    """Send text back to the client."""
    try:
      self.sock.sendall(text)
    except socket.error:
      self.sock.close()
      self.sock = None
  def Reply(self, *args):	# args is name, value, name, value, ..., int
    """Create a string reply of name, value pairs, and an ending integer code."""
    if self.extended:			# Use extended format
      t = "%s:" % self.cmd		# Extended format echoes the command and parameters
      for param in self.params:
        t = "%s %s" % (t, param)
      t += self.extended
      for i in range(0, len(args) - 1, 2):
        t = "%s%s: %s%c" % (t, args[i], args[i+1], self.extended)
      t += "RPRT %d\n" % args[-1]
    elif len(args) > 1:		# Use simple format
      t = ''
      for i in range(1, len(args) - 1, 2):
        t = "%s%s\n" % (t, args[i])
    else:		# No names; just the required integer code
      t = "RPRT %d\n" % args[0]
    # print 'Reply', t
    self.Send(t)
  def ErrParam(self):		# Invalid parameter
    self.Reply(-1)
  def UnImplemented(self):	# Command not implemented
    self.Reply(-4)
  def ErrProtocol(self):	# Protocol error
    self.Reply(-8)
  def Process(self):
    """This is the main processing loop, and is called frequently.  It reads and satisfies requests."""
    if not self.sock:
      return 0
    try:	# Read any data from the socket
      text = self.sock.recv(1024)
    except socket.timeout:	# This does not work
      pass
    except socket.error:	# Nothing to read
      pass
    else:					# We got some characters
      self.received += text
    if '\n' in self.received:	# A complete command ending with newline is available
      cmd, self.received = self.received.split('\n', 1)	# Split off the command, save any further characters
    else:
      return 1
    cmd = cmd.strip()		# Here is our command
    # print 'Get', cmd
    if not cmd:			# ??? Indicates a closed connection?
      # print 'empty command'
      self.sock.close()
      self.sock = None
      return 0
    # Parse the command and call the appropriate handler
    if cmd[0] == '+':			# rigctld Extended Response Protocol
      self.extended = '\n'
      cmd = cmd[1:].strip()
    elif cmd[0] in ';|,':		# rigctld Extended Response Protocol
      self.extended = cmd[0]
      cmd = cmd[1:].strip()
    else:
      self.extended = None
    if cmd[0:1] == '\\':		# long form command starting with backslash
      args = cmd[1:].split()
      self.cmd = args[0]
      self.params = args[1:]
      self.Handlers.get(self.cmd, self.UnImplemented)()
    else:						# single-letter command
      self.params = cmd[1:].strip()
      cmd = cmd[0:1]
      if cmd in 'Qq':	# Quit command
        return 0
      try:
        t = self.SingleLetters[cmd.lower()]
      except KeyError:
        self.UnImplemented()
      else:
        if cmd in string.uppercase:
          self.cmd = 'set_' + t
        else:
          self.cmd = 'get_' + t
        self.Handlers.get(self.cmd, self.UnImplemented)()
    return 1
  # These are the handlers for each request
  def DumpState(self):
    self.Send(self.dump2)
  def GetFreq(self):
    self.Reply('Frequency', self.app.rxFreq + self.app.VFO, 0)
  def SetFreq(self):
    try:
      freq = float(self.params)
      self.Reply(0)
    except:
      self.ErrParam()
    else:
      freq = int(freq + 0.5)
      self.app.ChangeRxTxFrequency(freq, None)
  def GetSplitFreq(self):
    self.Reply('TX Frequency', self.app.txFreq + self.app.VFO, 0)
  def SetSplitFreq(self):
    try:
      freq = float(self.params)
      self.Reply(0)
    except:
      self.ErrParam()
    else:
      freq = int(freq + 0.5)
      self.app.ChangeRxTxFrequency(None, freq)
  def GetSplitVfo(self):
    # I am not sure if "VFO" is a suitable response
    if self.app.split_rxtx:
      self.Reply('Split', 1, 'TX VFO', 'VFO', 0)
    else:
      self.Reply('Split', 0, 'TX VFO', 'VFO', 0)
  def SetSplitVfo(self):
    # Currently (Aug 2012) hamlib fails to send the "split" parameter, so this fails
    try:
      split, vfo = self.params.split()
      split = int(split)
      self.Reply(0)
    except:
      # traceback.print_exc()
      self.ErrParam()
    else:
      self.app.splitButton.SetValue(split, True)
  def GetInfo(self):
    self.Reply("Info", self.app.main_frame.GetTitle(), 0)
  def GetMode(self):
    mode = self.app.mode
    if mode == 'CWU':
      mode = 'CW'
    elif mode == 'CWL':		# Is this what CWR means?
      mode = 'CWR'
    elif mode[0:4] == 'DGT-':
      mode = 'USB'
    self.Reply('Mode', mode, 'Passband', self.app.filter_bandwidth, 0)
  def SetMode(self):
    try:
      mode, bw = self.params.split()
      bw = int(float(bw) + 0.5)
    except:
      self.ErrParam()
      return
    if mode in ('USB', 'LSB', 'AM', 'FM'):
      self.Reply(0)
    elif mode[0:4] == 'DGT-':
      self.Reply(0)
    elif mode == 'CW':
      mode = 'CWU'
      self.Reply(0)
    elif mode == 'CWR':
      mode = 'CWL'
      self.Reply(0)
    else:
      self.ErrParam()
      return
    self.app.OnBtnMode(None, mode)		# Set mode
    if bw <= 0:		# use default bandwidth
      return
    # Choose button closest to requested bandwidth
    buttons = self.app.filterButns.GetButtons()
    Lab = buttons[0].GetLabel()
    diff = abs(int(Lab) - bw)
    for i in range(1, len(buttons) - 1):
      label = buttons[i].GetLabel()
      df = abs(int(label) - bw)
      if df < diff:
        Lab = label
        diff = df
    self.app.OnBtnFilter(None, int(Lab))
  def GetVfo(self):
    self.Reply('VFO', "VFO", 0)		# The type of VFO we have
  def GetPtt(self):
    if QS.is_key_down():
      self.Reply('PTT', 1, 0)
    else:
      self.Reply('PTT', 0, 0)
  def SetPtt(self):
    if not self.app.pttButton:
      self.UnImplemented()
      return
    try:
      ptt = int(self.params)
      self.Reply(0)
    except:
      self.ErrParam()
    else:
      self.app.pttButton.SetValue(ptt, True)

class SoundThread(threading.Thread):
  """Create a second (non-GUI) thread to read, process and play sound."""
  def __init__(self):
    self.do_init = 1
    threading.Thread.__init__(self)
    self.doQuit = threading.Event()
    self.doQuit.clear()
  def run(self):
    """Read, process, play sound; then notify the GUI thread to check for FFT data."""
    if self.do_init:	# Open sound using this thread
      self.do_init = 0
      QS.start_sound()
      wx.CallAfter(application.PostStartup)
    while not self.doQuit.isSet():
      QS.read_sound()
      wx.CallAfter(application.OnReadSound)
    QS.close_sound()
  def stop(self):
    """Set a flag to indicate that the sound thread should end."""
    self.doQuit.set()

class ConfigScreen(wx.Panel):
  """Display a notebook with status and configuration data"""
  def __init__(self, parent, width, fft_size):
    self.y_scale = 0
    self.y_zero = 0
    wx.Panel.__init__(self, parent)
    self.notebook = notebook = wx.Notebook(self)
    self.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.OnPageChanged)
    notebook.SetBackgroundColour(conf.color_graph)
    self.SetBackgroundColour(conf.color_config2)
    font = wx.Font(conf.config_font_size, wx.FONTFAMILY_SWISS, wx.NORMAL,
          wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface)
    notebook.SetFont(font)
    notebook.SetForegroundColour(conf.color_notebook_txt)
    sizer = wx.BoxSizer()
    sizer.Add(notebook, 1, wx.EXPAND)
    self.SetSizer(sizer)
    # create the page windows
    self.status = ConfigStatus(notebook, width, fft_size)
    notebook.AddPage(self.status, "Status")
    self.config = ConfigConfig(notebook, width)
    notebook.AddPage(self.config, "Config")
    self.sound = ConfigSound(notebook, width)
    notebook.AddPage(self.sound, "Sound")
    self.favorites = ConfigFavorites(notebook, width)
    notebook.AddPage(self.favorites, "Favorites")
    self.tx_audio = ConfigTxAudio(notebook, width)
    notebook.AddPage(self.tx_audio, "Tx Audio")
    self.tx_audio.status = self.status
  def ChangeYscale(self, y_scale):
    pass
  def ChangeYzero(self, y_zero):
    pass
  def OnIdle(self, event):
    pass
  def SetTxFreq(self, tx_freq, rx_freq):
    pass
  def OnGraphData(self, data=None):
    self.status.OnGraphData(data)
    self.tx_audio.OnGraphData(data)
  def InitBitmap(self):		# Initial construction of bitmap
    self.status.InitBitmap()
  def OnPageChanged(self, event=None):
    if event:
      tab = event.GetSelection()
    else:
      tab = self.notebook.GetSelection()

class ConfigStatus(wx.Panel):
  """Display the status screen."""
  def __init__(self, parent, width, fft_size):
    wx.Panel.__init__(self, parent)
    self.Bind(wx.EVT_PAINT, self.OnPaint)
    self.width = width
    self.fft_size = fft_size
    self.interupts = 0
    self.read_error = -1
    self.write_error = -1
    self.underrun_error = -1
    self.fft_error = -1
    self.latencyCapt = -1
    self.latencyPlay = -1
    self.y_scale = 0
    self.y_zero = 0
    self.rate_min = -1
    self.rate_max = -1
    self.chan_min = -1
    self.chan_max = -1
    self.mic_max_display = 0
    self.err_msg = "No response"
    self.msg1 = ""
    self.font = wx.Font(conf.status_font_size, wx.FONTFAMILY_SWISS, wx.NORMAL,
          wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface)
    self.SetFont(self.font)
    charx = self.charx = self.GetCharWidth()
    chary = self.chary = self.GetCharHeight()
    self.dy = chary		# line spacing
    self.rjustify1 = (0, 1, 0)
    self.tabstops1 = [0] * 3
    self.tabstops1[0] = x = charx
    self.tabstops1[1] = x = x + self.GetTextExtent("FFT number of errors 1234567890")[0]
    self.tabstops1[2] = x = x + self.GetTextExtent("XXXX")[0]
    self.rjustify2 = (0, 0, 1, 1, 1)
    self.tabstops2 = []
  def MakeTabstops(self):
    luse = lname = 0
    for use, name, rate, latency, errors in QS.sound_errors():
      w, h = self.GetTextExtent(use)
      luse = max(luse, w)
      w, h = self.GetTextExtent(name)
      lname = max(lname, w)
    if luse == 0:
      return
    charx = self.charx
    self.tabstops2 = [0] * 5
    self.tabstops2[0] = x = charx
    self.tabstops2[1] = x = x + luse + charx * 6
    self.tabstops2[2] = x = x + lname + self.GetTextExtent("Sample rateXXXXXX")[0]
    self.tabstops2[3] = x = x + charx * 12
    self.tabstops2[4] = x = x + charx * 12
  def OnPaint(self, event):
    # Make and blit variable data
    self.MakeBitmap()
    dc = wx.PaintDC(self)
    dc.Blit(0, 0, self.mem_width, self.mem_height, self.mem_dc, 0, 0)
  def MakeRow2(self, *args):
    for col in range(len(args)):
      t = args[col]
      if t is None:
        continue
      t = str(t)
      x = self.tabstops[col]
      if self.rjustify[col]:
        w, h = self.mem_dc.GetTextExtent(t)
        x -= w
      if "Error" in t and t != "Errors":
        self.mem_dc.SetTextForeground('Red')
        self.mem_dc.DrawText(t, x, self.mem_y)
        self.mem_dc.SetTextForeground(conf.color_graphlabels)
      else:
        self.mem_dc.DrawText(t, x, self.mem_y)
    self.mem_y += self.dy
  def InitBitmap(self):		# Initial construction of bitmap
    self.mem_height = application.screen_height
    self.mem_width = application.screen_width
    self.bitmap = wx.EmptyBitmap(self.mem_width, self.mem_height)
    self.mem_dc = wx.MemoryDC()
    self.mem_rect = wx.Rect(0, 0, self.mem_width, self.mem_height)
    self.mem_dc.SelectObject(self.bitmap)
    br = wx.Brush(conf.color_graph)
    self.mem_dc.SetBackground(br)
    self.mem_dc.SetFont(self.font)
    self.mem_dc.SetTextForeground(conf.color_graphlabels)
    self.mem_dc.Clear()
  def MakeBitmap(self):
    self.mem_dc.Clear()
    self.mem_y = self.charx
    self.tabstops = self.tabstops1
    self.rjustify = self.rjustify1
    if conf.config_file_exists:
      cfile = "Configuration file:  %s" % conf.config_file_path
    else:
      cfile = "Error: Configuration file not found %s" % conf.config_file_path
    if conf.microphone_name:
      level = "%3.0f" % self.mic_max_display
    else:
      level = "None"
    if self.err_msg:
      err_msg = self.err_msg
    else:
      err_msg = None
    self.MakeRow2("Sample interrupts", self.interupts, cfile)
    self.MakeRow2("Microphone level dB", level, application.config_text)
    self.MakeRow2("FFT number of points", self.fft_size, err_msg)
    if conf.dxClHost:		# connection to dx cluster
      nSpots = len(application.dxCluster.dxSpots)
      if nSpots > 0:
        msg = str(nSpots) + ' DX spot' + ('' if nSpots==1 else 's') + ' received from ' + application.dxCluster.getHost()
      else:
        msg = "No DX Cluster data from %s" % conf.dxClHost
      self.MakeRow2("FFT number of errors", self.fft_error, msg)
    else:
      self.MakeRow2("FFT number of errors", self.fft_error)
    self.mem_y += self.dy
    if not self.tabstops2:
      return
    self.tabstops = self.tabstops2
    self.rjustify = self.rjustify2
    self.font.SetUnderlined(True)
    self.mem_dc.SetFont(self.font)
    self.MakeRow2("Device", "Name", "Sample rate", "Latency", "Errors")
    self.font.SetUnderlined(False)
    self.mem_dc.SetFont(self.font)
    self.mem_y += self.dy * 3 // 10
    if conf.use_sdriq:
      self.MakeRow2("Capture radio samples", "SDR-IQ", application.sample_rate, self.latencyCapt, self.read_error)
    elif conf.use_rx_udp:
      self.MakeRow2("Capture radio samples", "UDP", application.sample_rate, self.latencyCapt, self.read_error)
    for use, name, rate, latency, errors in QS.sound_errors():
      self.MakeRow2(use, name, rate, latency, errors)
  def OnGraphData(self, data=None):
    if not self.tabstops2:      # Must wait for sound to start
      self.MakeTabstops()
    (self.rate_min, self.rate_max, sample_rate, self.chan_min, self.chan_max,
         self.msg1, self.unused, self.err_msg,
         self.read_error, self.write_error, self.underrun_error,
         self.latencyCapt, self.latencyPlay, self.interupts, self.fft_error, self.mic_max_display,
         self.data_poll_usec
	 ) = QS.get_state()
    self.mic_max_display = 20.0 * math.log10((self.mic_max_display + 1) / 32767.0)
    self.RefreshRect(self.mem_rect)

class ConfigConfig(wx.Panel):
  def __init__(self, parent, width):
    wx.Panel.__init__(self, parent)
    self.width = width
    self.SetBackgroundColour(conf.color_graph)
    self.SetForegroundColour(conf.color_graphlabels)
    self.font = wx.Font(conf.config_font_size, wx.FONTFAMILY_SWISS, wx.NORMAL,
          wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface)
    self.SetFont(self.font)
    self.charx = charx = self.GetCharWidth()
    self.chary = chary = self.GetCharHeight()
    self.dy = self.chary
    self.rx_phase = None
    # Make controls
    tab0 = charx * 4
    # Receive phase
    rx = wx.StaticText(self, -1, "Adjust receive amplitude and phase")
    tx = wx.StaticText(self, -1, "Adjust transmit amplitude and phase")
    x1, y1 = tx.GetSizeTuple()
    self.rx_phase = ctrl = wx.Button(self, -1, "Rx Phase...")
    self.Bind(wx.EVT_BUTTON, self.OnBtnPhase, ctrl)
    if not conf.name_of_sound_capt:
      ctrl.Enable(0)
    x2, y2 = ctrl.GetSizeTuple()
    tab1 = tab0 + x1 + charx * 2
    tab2 = tab1 + x2
    tab3 = tab2 + charx * 8
    self.y = y2 + self.chary
    self.dy = y2 * 12 // 10
    self.offset = (y2 - y1) // 2
    rx.SetPosition((tab0, self.y))
    ctrl.SetPosition((tab1, self.y - self.offset))
    # File for recording speaker audio
    b = wx.Button(self, -1, "File...", pos=(tab3, self.y - self.offset))
    self.Bind(wx.EVT_BUTTON, self.OnBtnFileAudio, b)
    x3, y3 = b.GetSizeTuple()
    tab4 = tab3 + x3 + charx
    self.static_audio_text = "Record audio to WAV file "
    self.static_audio_path = conf.file_name_audio
    self.static_audio = wx.StaticText(self, -1, self.static_audio_text + self.static_audio_path, pos=(tab4, self.y))
    QS.set_file_record(0, self.static_audio_path)
    self.y += self.dy
    # Transmit phase
    self.tx_phase = ctrl = wx.Button(self, -1, "Tx Phase...")
    self.Bind(wx.EVT_BUTTON, self.OnBtnPhase, ctrl)
    if not conf.name_of_mic_play:
      ctrl.Enable(0)
    tx.SetPosition((tab0, self.y))
    ctrl.SetPosition((tab1, self.y - self.offset))
    # File for recording samples
    b = wx.Button(self, -1, "File...", pos=(tab3, self.y - self.offset))
    self.Bind(wx.EVT_BUTTON, self.OnBtnFileSamples, b)
    self.static_samples_text = "Record samples to WAV file "
    self.static_samples_path = conf.file_name_samples
    self.static_samples = wx.StaticText(self, -1, self.static_samples_text + self.static_samples_path, pos=(tab4, self.y))
    QS.set_file_record(1, self.static_samples_path)
    self.y += self.dy
    # Choice (combo) box for decimation
    lst = Hardware.VarDecimGetChoices()
    if lst:
      txt = Hardware.VarDecimGetLabel()
      index = Hardware.VarDecimGetIndex()
    else:
      txt = "Variable decimation"
      lst = ["None"]
      index = 0
    t = wx.StaticText(self, -1, txt)
    ctrl = wx.Choice(self, -1, choices=lst, size=(x2, y2))
    if lst:
      self.Bind(wx.EVT_CHOICE, application.OnBtnDecimation, ctrl)
      ctrl.SetSelection(index)
    t.SetPosition((tab0, self.y))
    ctrl.SetPosition((tab1, self.y - self.offset))
    self.y += self.dy
    # Transmit level controls
    if hasattr(Hardware, "SetTxLevel"):
      SliderBoxH(self, "Tx level %d%%  ", 100, 0, 100, self.OnTxLevel, True, (tab0, self.y), tab2-tab0)
      level = conf.digital_tx_level
      SliderBoxH(self, "Digital Tx level %d%%  ", level, 0, level, self.OnDigitalTxLevel, True, (tab3, self.y), tab2-tab0)
      self.y += self.dy
    # mic_out_volume
    level = int(conf.mic_out_volume * 100.0 + 0.1)
    SliderBoxH(self, "SftRock Tx level %d%%  ", level, 0, 100, self.OnSrTxLevel, True, (tab0, self.y), tab2-tab0)
    self.y += self.dy
  def OnTxLevel(self, event):
    application.tx_level = event.GetEventObject().GetValue()
    Hardware.SetTxLevel()
  def OnSrTxLevel(self, event):
    level = event.GetEventObject().GetValue()
    QS.set_mic_out_volume(level)
  def OnDigitalTxLevel(self, event):
    application.digital_tx_level = event.GetEventObject().GetValue()
    Hardware.SetTxLevel()
  def OnBtnPhase(self, event):
    btn = event.GetEventObject()
    if btn.GetLabel()[0:2] == 'Tx':
      rx_tx = 'tx'
    else:
      rx_tx = 'rx'
    application.screenBtnGroup.SetLabel('Graph', do_cmd=True)
    if application.w_phase:
      application.w_phase.Raise()
    else:
      application.w_phase = QAdjustPhase(self, self.width, rx_tx)
  def OnBtnFileAudio(self, event):
    dr, fn = os.path.split(self.static_audio_path)
    dlg = wx.FileDialog(self, 'Choose WAV file', dr, fn, style=wx.FD_SAVE|wx.FD_OVERWRITE_PROMPT,
      wildcard="Wave files (*.wav)|*.wav")
    if dlg.ShowModal() == wx.ID_OK:
      path = dlg.GetPath()
      if path[-4:].lower() != '.wav':
        path = path + '.wav'
      QS.set_file_record(0, path)
      application.btn_file_record.Enable()
      self.static_audio.SetLabel(self.static_audio_text + path)
      self.static_audio_path = path
    dlg.Destroy()
  def OnBtnFileSamples(self, event):
    dr, fn = os.path.split(self.static_samples_path)
    dlg = wx.FileDialog(self, 'Choose WAV file', dr, fn, style=wx.FD_SAVE|wx.FD_OVERWRITE_PROMPT,
      wildcard="Wave files (*.wav)|*.wav")
    if dlg.ShowModal() == wx.ID_OK:
      path = dlg.GetPath()
      if path[-4:].lower() != '.wav':
        path = path + '.wav'
      QS.set_file_record(1, path)
      application.btn_file_record.Enable()
      self.static_samples.SetLabel(self.static_samples_text + path)
      self.static_samples_path = path
    dlg.Destroy()

class ConfigSound(wx.ScrolledWindow):
  """Display the available sound devices."""
  def __init__(self, parent, width):
    wx.ScrolledWindow.__init__(self, parent)
    self.Bind(wx.EVT_PAINT, self.OnPaint)
    self.width = width
    self.dev_capt, self.dev_play = QS.sound_devices()
    self.SetBackgroundColour(conf.color_graph)
    self.font = wx.Font(conf.config_font_size, wx.FONTFAMILY_SWISS, wx.NORMAL,
          wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface)
    self.SetFont(self.font)
    self.charx = self.GetCharWidth()
    self.chary = self.GetCharHeight()
    self.dy = self.chary
    height = self.chary * (3 + len(self.dev_capt) + len(self.dev_play))
    if sys.platform != 'win32' and conf.show_pulse_audio_devices:
      self.pa_dev_capt, self.pa_dev_play = QS.pa_sound_devices()
      height += self.chary * (3 + 3 * len(self.pa_dev_capt) + 3 * len(self.pa_dev_play))
    self.SetScrollbars(1, 1, 100, height)
  def OnPaint(self, event):
    dc = wx.PaintDC(self)
    self.DoPrepareDC(dc)
    dc.SetFont(self.font)
    dc.SetTextForeground(conf.color_graphlabels)
    x0 = self.charx
    self.y = self.chary // 3
    dc.DrawText("Available devices for capture:", x0, self.y)
    self.y += self.dy
    for name in self.dev_capt:
      dc.DrawText('    ' + name, x0, self.y)
      self.y += self.dy
    dc.DrawText("Available devices for playback:", x0, self.y)
    self.y += self.dy
    for name in self.dev_play:
      dc.DrawText('    ' + name, x0, self.y)
      self.y += self.dy
    if sys.platform != 'win32' and conf.show_pulse_audio_devices:
      dc.DrawText("Available PulseAudio devices for capture (sources):", x0, self.y)
      self.y += self.dy
      for n0, n1, n2 in self.pa_dev_capt:
        dc.DrawText(' ' * 4 + n1, x0, self.y)
        self.y += self.dy
        dc.DrawText(' ' * 8 + n0, x0, self.y)
        self.y += self.dy
        if n2:
          dc.DrawText(' ' * 8 + n2, x0, self.y)
          self.y += self.dy
      dc.DrawText("Available PulseAudio devices for playback (sinks):", x0, self.y)
      self.y += self.dy
      for n0, n1, n2 in self.pa_dev_play:
        dc.DrawText(' ' * 4 + n1, x0, self.y)
        self.y += self.dy
        dc.DrawText(' ' * 8 + n0, x0, self.y)
        self.y += self.dy
        if n2:
          dc.DrawText(' ' * 8 + n2, x0, self.y)
          self.y += self.dy

class ConfigFavorites(wx.grid.Grid):
  def __init__(self, parent, width):
    wx.grid.Grid.__init__(self, parent)
    self.changed = False
    self.SetBackgroundColour(conf.color_graph)
    self.SetForegroundColour(conf.color_graphlabels)
    font = wx.Font(conf.favorites_font_size, wx.FONTFAMILY_SWISS, wx.NORMAL,
          wx.FONTWEIGHT_BOLD, face=conf.quisk_typeface)
    self.SetFont(font)
    self.SetLabelFont(font)
    font = wx.Font(conf.favorites_font_size, wx.FONTFAMILY_SWISS, wx.NORMAL,
          wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface)
    self.SetDefaultCellFont(font)
    self.SetDefaultCellBackgroundColour(conf.color_graph)
    self.SetDefaultCellTextColour(conf.color_graphlabels)
    self.SetLabelBackgroundColour(conf.color_graph)
    self.SetLabelTextColour(conf.color_graphlabels)
    self.SetGridLineColour(conf.color_graphlabels)
    self.SetDefaultRowSize(self.GetCharHeight()+3)
    self.Bind(wx.grid.EVT_GRID_LABEL_RIGHT_CLICK, self.OnRightClickLabel)
    self.Bind(wx.grid.EVT_GRID_CELL_CHANGE, self.OnChange)
    self.Bind(wx.grid.EVT_GRID_LABEL_LEFT_DCLICK, self.OnLeftDClick)
    self.CreateGrid(0, 4)
    self.EnableDragRowSize(False)
    w = self.GetTextExtent(' 999 ')[0]
    self.SetRowLabelSize(w)
    self.SetColLabelValue(0,  'Name')
    self.SetColLabelValue(1,  'Frequency')
    self.SetColLabelValue(2,  'Mode')
    self.SetColLabelValue(3,  'Description')
    w = self.GetTextExtent("xFrequencyx")[0]
    self.SetColSize(0, w * 3 // 2)
    self.SetColSize(1, w)
    self.SetColSize(2, w // 2)
    self.SetColSize(3, width - w * 3 - self.GetRowLabelSize() - 20)
    if conf.favorites_file_path:
      self.init_path = conf.favorites_file_path
    else:
      self.init_path = os.path.join(os.path.dirname(ConfigPath), 'quisk_favorites.txt')
    self.ReadIn()
    if self.GetNumberRows() < 1:
      self.AppendRows()
    # Make a popup menu
    self.popupmenu = wx.Menu()
    item = self.popupmenu.Append(-1, 'Tune to')
    self.Bind(wx.EVT_MENU, self.OnPopupTuneto, item)
    self.popupmenu.AppendSeparator()
    item = self.popupmenu.Append(-1, 'Append')
    self.Bind(wx.EVT_MENU, self.OnPopupAppend, item)
    item = self.popupmenu.Append(-1, 'Insert')
    self.Bind(wx.EVT_MENU, self.OnPopupInsert, item)
    item = self.popupmenu.Append(-1, 'Delete')
    self.Bind(wx.EVT_MENU, self.OnPopupDelete, item)
    self.popupmenu.AppendSeparator()
    item = self.popupmenu.Append(-1, 'Move Up')
    self.Bind(wx.EVT_MENU, self.OnPopupMoveUp, item)
    item = self.popupmenu.Append(-1, 'Move Down')
    self.Bind(wx.EVT_MENU, self.OnPopupMoveDown, item)
    # Make a timer
    self.timer = wx.Timer(self)
    self.Bind(wx.EVT_TIMER, self.OnTimer)
  def ReadIn(self):
    try:
      fp = open(self.init_path, 'rb')
      lines = fp.readlines()
      fp.close()
    except:
      lines = ("my net|7210000|LSB|My net 2030 UTC every Thursday",
               "10m FM 1|29.620|FM|Fm local 10 meter repeater")
    for row in range(len(lines)):
      self.AppendRows()
      fields = lines[row].split('|')
      for col in range(len(fields)):
        self.SetCellValue(row, col, fields[col].strip())
  def WriteOut(self):
    self.changed = False
    try:
      fp = open(self.init_path, 'wb')
    except:
      return
    for row in range(self.GetNumberRows()):
      t = self.GetCellValue(row, 0)
      for col in range(1, self.GetNumberCols()):
        cell = self.GetCellValue(row, col)
        cell = cell.replace('|', ';')
        t = "%s | %s" % (t, cell)
      t = t + '\r\n'
      fp.write(t)
    fp.close()
  def AddNewFavorite(self):
    self.InsertRows(0)
    self.SetCellValue(0, 0, 'New station');
    self.SetCellValue(0, 1, str(application.rxFreq + application.VFO))
    self.SetCellValue(0, 2, application.mode);
    self.OnChange()
  def OnRightClickLabel(self, event):
    event.Skip()
    self.menurow = event.GetRow()
    if self.menurow >= 0:
      pos = event.GetPosition()
      self.PopupMenu(self.popupmenu, pos)
  def OnLeftDClick(self, event):		# Thanks to Christof, DJ4CM
    self.menurow = event.GetRow()
    self.OnPopupTuneto(event)
  def OnPopupAppend(self, event):
    self.InsertRows(self.menurow + 1)
    self.OnChange()
  def OnPopupInsert(self, event):
    self.InsertRows(self.menurow)
    self.OnChange()
  def OnPopupDelete(self, event):
    self.DeleteRows(self.menurow)
    if self.GetNumberRows() < 1:
      self.AppendRows()
    self.OnChange()
  def OnPopupMoveUp(self, event):
    row = self.menurow
    if row < 1:
      return
    for i in range(4):
      c = self.GetCellValue(row - 1, i)
      self.SetCellValue(row - 1, i, self.GetCellValue(row, i))
      self.SetCellValue(row, i, c)
  def OnPopupMoveDown(self, event):
    row = self.menurow
    if row == self.GetNumberRows() - 1:
      return
    for i in range(4):
      c = self.GetCellValue(row + 1, i)
      self.SetCellValue(row + 1, i, self.GetCellValue(row, i))
      self.SetCellValue(row, i, c)
  def OnPopupTuneto(self, event):
    freq = self.GetCellValue(self.menurow, 1)
    if not freq:
      return
    try:
      freq = str2freq (freq)
    except ValueError:
      print('Bad frequency')
      return
    if self.changed:
      if self.timer.IsRunning():
        self.timer.Stop()
      self.WriteOut()
    application.ChangeRxTxFrequency(None, freq)
    mode = self.GetCellValue(self.menurow, 2)
    mode = mode.upper()
    application.OnBtnMode(None, mode)
    application.screenBtnGroup.SetLabel(conf.default_screen, do_cmd=True)
  def OnChange(self, event=None):
    self.changed = True
    if self.timer.IsRunning():
      self.timer.Stop()
    self.timer.Start(5000, oneShot=True)
  def OnTimer(self, event):
    if self.changed:
      self.WriteOut()

class ConfigTxAudio(wx.Panel):
  """Display controls for the transmit audio."""
  def __init__(self, parent, width):
    wx.Panel.__init__(self, parent)
    self.width = width
    self.SetBackgroundColour(conf.color_graph)
    self.SetForegroundColour(conf.color_graphlabels)
    self.font = wx.Font(conf.config_font_size, wx.FONTFAMILY_SWISS, wx.NORMAL,
          wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface)
    self.SetFont(self.font)
    self.charx = charx = self.GetCharWidth()
    self.chary = chary = self.GetCharHeight()
    self.tmp_playing = False
    # Make controls
    tab0 = charx * 4
    self.y = chary
    t = "This is a test screen for transmit audio.  SSB, AM and FM have separate settings."
    wx.StaticText(self, -1, t, pos=(tab0, self.y))
    self.btn_record = wx.lib.buttons.GenToggleButton(self, -1, "Record")
    x2, y2 = self.btn_record.GetSizeTuple()
    self.dy = y2 * 12 // 10
    self.offset = (y2 - chary) // 2
    self.y += self.dy
    # Record and Playback
    ctl = wx.StaticText(self, -1, "Listen to transmit audio", pos=(tab0, self.y))
    x1, y1 = ctl.GetSizeTuple()
    x = tab0 + x1 + charx * 3
    y = self.y - self.offset
    self.btn_record.SetPosition((x, y))
    self.btn_playback = wx.lib.buttons.GenToggleButton(self, -1, "Playback", pos=(x + x2 + charx * 3, y))
    self.btn_playback.Enable(0)
    if not conf.microphone_name:
      self.btn_record.Enable(0)
    tab1 = x + x2
    tab2 = tab1 + charx * 3
    self.Bind(wx.EVT_BUTTON, self.OnBtnRecord, self.btn_record)
    self.Bind(wx.EVT_BUTTON, self.OnBtnPlayback, self.btn_playback)
    self.y += self.dy
    # mic level
    self.mic_text = wx.StaticText(self, -1, "Peak microphone audio level   None", pos=(tab0, self.y))
    t = "Adjust the peak audio level to a few dB below zero."
    wx.StaticText(self, -1, t, pos=(tab2, self.y))
    self.y += self.dy
    # Vox level
    SliderBoxH(self, "VOX %d dB  ", application.levelVOX, -40, 0, application.OnLevelVOX, True, (tab0, self.y), tab1-tab0)
    t = "Audio level that triggers VOX (all modes)."
    wx.StaticText(self, -1, t, pos=(tab2, self.y))
    self.y += self.dy
    # VOX hang
    SliderBoxH(self, "VOX %0.2f  ", application.timeVOX, 0, 4000, application.OnTimeVOX, True, (tab0, self.y), tab1-tab0, 0.001)
    t = "Time to hold VOX after end of audio in seconds."
    wx.StaticText(self, -1, t, pos=(tab2, self.y))
    self.y += self.dy
    # Tx Audio clipping
    application.CtrlTxAudioClip = SliderBoxH(self, "Clip %2d  ", 0, 0, 20, application.OnTxAudioClip, True, (tab0, self.y), tab1-tab0)
    t = "Tx audio clipping level in dB for this mode."
    wx.StaticText(self, -1, t, pos=(tab2, self.y))
    self.y += self.dy
    # Tx Audio preemphasis
    application.CtrlTxAudioPreemph = SliderBoxH(self, "Preemphasis %4.2f  ", 0, 0, 100, application.OnTxAudioPreemph, True, (tab0, self.y), tab1-tab0, 0.01)
    t = "Tx audio preemphasis of high frequencies."
    wx.StaticText(self, -1, t, pos=(tab2, self.y))
    self.y += self.dy
  def OnGraphData(self, data=None):
    if conf.microphone_name:
      txt = "Peak microphone audio level %3.0f dB" % self.status.mic_max_display
      self.mic_text.SetLabel(txt)
    if self.tmp_playing and QS.set_record_state(-1):	# poll to see if playback is finished
      self.tmp_playing = False
      self.btn_playback.SetValue(False)
      self.btn_record.Enable(1)
  def OnBtnRecord(self, event):
    if event.GetEventObject().GetValue():
      QS.set_kill_audio(1)
      self.btn_playback.Enable(0)
      QS.set_record_state(4)
    else:
      QS.set_kill_audio(0)
      self.btn_playback.Enable(1)
      QS.set_record_state(1)
  def OnBtnPlayback(self, event):
    if event.GetEventObject().GetValue():
      self.btn_record.Enable(0)
      QS.set_record_state(2)
      self.tmp_playing = True
    else:
      self.btn_record.Enable(1)
      QS.set_record_state(3)
      self.tmp_playing = False

class GraphDisplay(wx.Window):
  """Display the FFT graph within the graph screen."""
  def __init__(self, parent, x, y, graph_width, height, chary):
    wx.Window.__init__(self, parent,
       pos = (x, y),
       size = (graph_width, height),
       style = wx.NO_BORDER)
    self.parent = parent
    self.chary = chary
    self.graph_width = graph_width
    self.display_text = ""
    self.line = [(0, 0), (1,1)]		# initial fake graph data
    self.SetBackgroundColour(conf.color_graph)
    self.Bind(wx.EVT_PAINT, self.OnPaint)
    self.Bind(wx.EVT_LEFT_DOWN, parent.OnLeftDown)
    self.Bind(wx.EVT_RIGHT_DOWN, parent.OnRightDown)
    self.Bind(wx.EVT_LEFT_UP, parent.OnLeftUp)
    self.Bind(wx.EVT_MOTION, parent.OnMotion)
    self.Bind(wx.EVT_MOUSEWHEEL, parent.OnWheel)
    self.tune_tx = graph_width // 2	# Current X position of the Tx tuning line
    self.tune_rx = 0				# Current X position of Rx tuning line or zero
    self.scale = 20				# pixels per 10 dB
    self.peak_hold = 9999		# time constant for holding peak value
    self.height = 10
    self.y_min = 1000
    self.y_max = 0
    self.max_height = application.screen_height
    self.tuningPenTx = wx.Pen(conf.color_txline, 1)
    self.tuningPenRx = wx.Pen(conf.color_rxline, 1)
    self.backgroundPen = wx.Pen(self.GetBackgroundColour(), 1)
    self.backgroundBrush = wx.Brush(self.GetBackgroundColour())
    self.horizPen = wx.Pen(conf.color_gl, 1, wx.SOLID)
    self.font = wx.Font(conf.graph_font_size, wx.FONTFAMILY_SWISS, wx.NORMAL,
          wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface)
    self.SetFont(self.font)
    if sys.platform == 'win32':
      self.Bind(wx.EVT_ENTER_WINDOW, self.OnEnter)
    # This code displays the filter bandwidth on the graph screen.  It is based on code
    # provided by Terry Fox, WB4JFI.  Thanks Terry!
    self.fltr_disp_size = 1
    self.fltr_disp_tune = 0
    bitmap = wx.EmptyBitmap(graph_width, self.max_height)
    self.fltr_disp_tx_dc = wx.MemoryDC()
    self.fltr_disp_tx_dc.SelectObject(bitmap)
    br = wx.Brush(conf.color_bandwidth, wx.SOLID)
    self.fltr_disp_tx_dc.SetBackground(br)
    self.fltr_disp_tx_dc.SetPen(self.tuningPenTx)
    self.fltr_disp_tx_dc.DrawLine(0, 0, 0, self.max_height)
    bitmap = wx.EmptyBitmap(graph_width, self.max_height)
    self.fltr_disp_rx_dc = wx.MemoryDC()
    self.fltr_disp_rx_dc.SelectObject(bitmap)
    br = wx.Brush(conf.color_bandwidth, wx.SOLID)
    self.fltr_disp_rx_dc.SetBackground(br)
    self.fltr_disp_rx_dc.SetPen(self.tuningPenRx)
    self.fltr_disp_rx_dc.DrawLine(0, 0, 0, self.max_height)
  def OnEnter(self, event):
    if not application.w_phase:
      self.SetFocus()	# Set focus so we get mouse wheel events
  def OnPaint(self, event):
    #print 'GraphDisplay', self.GetUpdateRegion().GetBox()
    dc = wx.PaintDC(self)
    # Blit the tuning line and filter display to the screen
    dc.Blit(self.tune_tx - self.fltr_disp_tune, 0, self.fltr_disp_size, self.max_height, self.fltr_disp_tx_dc, 0, 0)
    if self.tune_rx:
      dc.Blit(self.tune_rx - self.fltr_disp_tune, 0, self.fltr_disp_size, self.max_height, self.fltr_disp_rx_dc, 0, 0)
    dc.SetPen(wx.Pen(conf.color_graphline, 1))
    dc.DrawLines(self.line)
    dc.SetPen(self.horizPen)
    for y in self.parent.y_ticks:
      dc.DrawLine(0, y, self.graph_width, y)	# y line
    if self.display_text:
      dc.SetFont(self.font)
      dc.SetTextForeground(conf.color_graphlabels)
      dc.DrawText(self.display_text, self.chary, self.chary)
  def SetHeight(self, height):
    self.height = height
    self.SetSize((self.graph_width, height))
  def OnGraphData(self, data):
    x = 0
    for y in data:	# y is in dB, -130 to 0
      y = self.zeroDB - int(y * self.scale / 10.0 + 0.5)
      try:
        y0 = self.line[x][1]
      except IndexError:
        self.line.append([x, y])
      else:
        if y > y0:
          y = min(y, y0 + self.peak_hold)
        self.line[x] = [x, y]
      x = x + 1
    self.Refresh()
  def UpdateFilterDisplay(self, size, tune):	# WB4JFI ADD - Update filter display
    self.fltr_disp_size = size
    self.fltr_disp_tune = tune
    self.fltr_disp_tx_dc.Clear()
    self.fltr_disp_tx_dc.DrawLine(tune, 0, tune, self.max_height)
    self.fltr_disp_rx_dc.Clear()
    self.fltr_disp_rx_dc.DrawLine(tune, 0, tune, self.max_height)
  def SetTuningLine(self, tune_tx, tune_rx):
    dc = wx.ClientDC(self)
    dc.SetPen(self.backgroundPen)
    dc.SetBrush(self.backgroundBrush)
    sz = self.fltr_disp_size
    xa = self.tune_tx - self.fltr_disp_tune
    dc.DrawRectangle(xa, 0, sz, self.max_height)
    xb = xa + sz
    if self.tune_rx:
      x = self.tune_rx - self.fltr_disp_tune
      dc.DrawRectangle(x, 0, sz, self.max_height)
      xa = min(xa, x)
      xb = max(xb, x + sz)
    x = tune_tx - self.fltr_disp_tune
    dc.Blit(x, 0, sz, self.max_height, self.fltr_disp_tx_dc, 0, 0)
    xa = min(xa, x)
    xb = max(xb, x + sz)
    if tune_rx:
      x = tune_rx - self.fltr_disp_tune
      dc.Blit(x, 0, sz, self.max_height, self.fltr_disp_rx_dc, 0, 0)
      xa = min(xa, x)
      xb = max(xb, x + sz)
    dc.SetPen(wx.Pen(conf.color_graphticks,1))
    dc.DrawLines(self.line[xa:xb])
    dc.SetPen(self.horizPen)
    for y in self.parent.y_ticks:
      dc.DrawLine(xa, y, xb, y)	# y line
    self.tune_tx = tune_tx
    self.tune_rx = tune_rx

class GraphScreen(wx.Window):
  """Display the graph screen X and Y axis, and create a graph display."""
  def __init__(self, parent, data_width, graph_width, in_splitter=0):
    wx.Window.__init__(self, parent, pos = (0, 0))
    self.in_splitter = in_splitter	# Are we in the top of a splitter window?
    if in_splitter:
      self.y_scale = conf.waterfall_graph_y_scale
      self.y_zero = conf.waterfall_graph_y_zero
    else:
      self.y_scale = conf.graph_y_scale
      self.y_zero = conf.graph_y_zero
    self.y_ticks = []
    self.VFO = 0
    self.mouse_x = 0
    self.WheelMod = conf.mouse_wheelmod		# Round frequency when using mouse wheel
    self.txFreq = 0
    self.sample_rate = application.sample_rate
    self.zoom = 1.0
    self.zoom_deltaf = 0
    self.data_width = data_width
    self.graph_width = graph_width
    self.doResize = False
    self.pen_tick = wx.Pen(conf.color_graphticks, 1)
    self.pen_label = wx.Pen(conf.color_graphlabels, 1)
    self.font = wx.Font(conf.graph_font_size, wx.FONTFAMILY_SWISS, wx.NORMAL,
          wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface)
    self.SetFont(self.font)
    w = self.GetCharWidth() * 14 // 10
    h = self.GetCharHeight()
    self.charx = w
    self.chary = h
    self.tick = max(2, h * 3 // 10)
    self.originX = w * 5
    self.offsetY = h + self.tick
    self.width = self.originX + self.graph_width + self.tick + self.charx * 2
    self.height = application.screen_height * 3 // 10
    self.x0 = self.originX + self.graph_width // 2		# center of graph
    self.tuningX = self.x0
    self.originY = 10
    self.zeroDB = 10	# y location of zero dB; may be above the top of the graph
    self.scale = 10
    self.SetSize((self.width, self.height))
    self.SetSizeHints(self.width, 1, self.width)
    self.SetBackgroundColour(conf.color_graph)
    self.Bind(wx.EVT_SIZE, self.OnSize)
    self.Bind(wx.EVT_PAINT, self.OnPaint)
    self.Bind(wx.EVT_LEFT_DOWN, self.OnLeftDown)
    self.Bind(wx.EVT_RIGHT_DOWN, self.OnRightDown)
    self.Bind(wx.EVT_LEFT_UP, self.OnLeftUp)
    self.Bind(wx.EVT_MOTION, self.OnMotion)
    self.Bind(wx.EVT_MOUSEWHEEL, self.OnWheel)
    self.MakeDisplay()
  def MakeDisplay(self):
    self.display = GraphDisplay(self, self.originX, 0, self.graph_width, 5, self.chary)
    self.display.zeroDB = self.zeroDB
  def OnPaint(self, event):
    dc = wx.PaintDC(self)
    dc.SetFont(self.font)
    dc.SetTextForeground(conf.color_graphlabels)
    if self.in_splitter:
      self.MakeYTicks(dc)
    else:
      self.MakeYTicks(dc)
      self.MakeXTicks(dc)
  def OnIdle(self, event):
    if self.doResize:
      self.ResizeGraph()
  def OnSize(self, event):
    self.doResize = True
    event.Skip()
  def ResizeGraph(self):
    """Change the height of the graph.

    Changing the width interactively is not allowed because the FFT size is fixed.
    Call after changing the zero or scale to recalculate the X and Y axis marks.
    """
    w, h = self.GetClientSize()
    if self.in_splitter:	# Splitter window has no X axis scale
      self.height = h
      self.originY = h
    else:
      self.height = h - self.chary		# Leave space for X scale
      self.originY = self.height - self.offsetY
    self.MakeYScale()
    self.display.SetHeight(self.originY)
    self.display.scale = self.scale
    self.doResize = False
    self.Refresh()
  def ChangeYscale(self, y_scale):
    self.y_scale = y_scale
    self.doResize = True
  def ChangeYzero(self, y_zero):
    self.y_zero = y_zero
    self.doResize = True
  def ChangeZoom(self, zoom, deltaf):
    self.zoom = zoom
    self.zoom_deltaf = deltaf
    self.doResize = True
  def MakeYScale(self):
    chary = self.chary
    scale = (self.originY - chary)  * 10 // (self.y_scale + 20)	# Number of pixels per 10 dB
    scale = max(1, scale)
    q = (self.originY - chary ) // scale // 2
    zeroDB = chary + q * scale - self.y_zero * scale // 10
    if zeroDB > chary:
      zeroDB = chary
    self.scale = scale
    self.zeroDB = zeroDB
    self.display.zeroDB = self.zeroDB
    QS.record_graph(self.originX, self.zeroDB, self.scale)
  def MakeYTicks(self, dc):
    chary = self.chary
    x1 = self.originX - self.tick * 3	# left of tick mark
    x2 = self.originX - 1		# x location of y axis
    x3 = self.originX + self.graph_width	# end of graph data
    dc.SetPen(self.pen_tick)
    dc.DrawLine(x2, 0, x2, self.originY + 1)	# y axis
    y = self.zeroDB
    del self.y_ticks[:]
    y_old = y
    for i in range(0, -99999, -10):
      if y >= chary // 2:
        dc.SetPen(self.pen_tick)
        dc.DrawLine(x1, y, x2, y)	# y tick
        self.y_ticks.append(y)
        t = repr(i)
        w, h = dc.GetTextExtent(t)
        # draw text on Y axis
        if y - y_old > h:
          if y + h // 2 <= self.originY:	
            dc.DrawText(repr(i), x1 - w, y - h // 2)
          elif h < self.scale:
            dc.DrawText(repr(i), x1 - w, self.originY - h)
          y_old = y
      y = y + self.scale
      if y >= self.originY - 3:
        break
  def MakeXTicks(self, dc):
    sample_rate = int(self.sample_rate * self.zoom)
    VFO = self.VFO + self.zoom_deltaf
    originY = self.originY
    x3 = self.originX + self.graph_width	# end of fft data
    charx , z = dc.GetTextExtent('-30000XX')
    tick0 = self.tick
    tick1 = tick0 * 2
    tick2 = tick0 * 3
    # Draw the X axis
    dc.SetPen(self.pen_tick)
    dc.DrawLine(self.originX, originY, x3, originY)
    # Draw the band plan colors below the X axis
    x = self.originX
    f = float(x - self.x0) * sample_rate / self.data_width
    c = None
    y = originY + 1
    for freq, color in conf.BandPlan:
      freq -= VFO
      if f < freq:
        xend = int(self.x0 + float(freq) * self.data_width / sample_rate + 0.5)
        if c is not None:
          dc.SetPen(wx.TRANSPARENT_PEN)
          dc.SetBrush(wx.Brush(c))
          dc.DrawRectangle(x, y, min(x3, xend) - x, tick0)  # x axis
        if xend >= x3:
          break
        x = xend
        f = freq
      c = color
    # check the width of the frequency label versus frequency span
    df = charx * sample_rate // self.data_width
    if VFO >= 10E9:     # Leave room for big labels
      df *= 1.33
    elif VFO >= 1E9:
      df *= 1.17
    # tfreq: tick frequency for labels in Hertz
    # stick: small tick in Hertz
    # mtick: medium tick
    # ltick: large tick
    s2 = 1000
    tfreq = None
    while tfreq is None:
      if df < s2:
        tfreq = s2
        stick = s2 / 10
        mtick = s2 / 2
        ltick = tfreq
      elif df < s2 * 2:
        tfreq = s2 * 2
        stick = s2 / 10
        mtick = s2 / 2
        ltick = s2
      elif df < s2 * 5:
        tfreq = s2 * 5
        stick = s2 / 2
        mtick = s2
        ltick = tfreq
      s2 *= 10
    # Draw the X axis ticks and frequency in kHz
    dc.SetPen(self.pen_tick)
    freq1 = VFO - sample_rate // 2
    freq1 = (freq1 // stick) * stick
    freq2 = freq1 + sample_rate + stick + 1
    y_end = 0
    for f in range (freq1, freq2, stick):
      x = self.x0 + int(float(f - VFO) / sample_rate * self.data_width)
      if self.originX <= x <= x3:
        if f % ltick == 0:		# large tick
          dc.DrawLine(x, originY, x, originY + tick2)
        elif f % mtick == 0:	# medium tick
          dc.DrawLine(x, originY, x, originY + tick1)
        else:					# small tick
          dc.DrawLine(x, originY, x, originY + tick0)
        if f % tfreq == 0:		# place frequency label
          t = str(f//1000)
          w, h = dc.GetTextExtent(t)
          dc.DrawText(t, x - w // 2, originY + tick2)
          y_end = originY + tick2 + h
    if y_end:		# mark the center of the display
      dc.DrawLine(self.x0, y_end, self.x0, application.screen_height)
  def OnGraphData(self, data):
    i1 = (self.data_width - self.graph_width) // 2
    i2 = i1 + self.graph_width
    self.display.OnGraphData(data[i1:i2])
  def SetVFO(self, vfo):
    self.VFO = vfo
    self.doResize = True
  def SetTxFreq(self, tx_freq, rx_freq):
    sample_rate = int(self.sample_rate * self.zoom)
    self.txFreq = tx_freq
    tx_x = self.x0 + int(float(tx_freq - self.zoom_deltaf) / sample_rate * self.data_width)
    self.tuningX = tx_x
    rx_x = self.x0 + int(float(rx_freq - self.zoom_deltaf) / sample_rate * self.data_width)
    if abs(tx_x - rx_x) < 2:		# Do not display Rx line for small frequency offset
      self.display.SetTuningLine(tx_x - self.originX, 0)
    else:
      self.display.SetTuningLine(tx_x - self.originX, rx_x - self.originX)
  def GetMousePosition(self, event):
    """For mouse clicks in our display, translate to our screen coordinates."""
    mouse_x, mouse_y = event.GetPositionTuple()
    win = event.GetEventObject()
    if win is not self:
      x, y = win.GetPositionTuple()
      mouse_x += x
      mouse_y += y
    return mouse_x, mouse_y
  def FreqRound(self, tune, vfo):
    if conf.freq_spacing:
      freq = tune + vfo
      n = int(freq) - conf.freq_base
      if n >= 0:
        n = (n + conf.freq_spacing // 2) // conf.freq_spacing
      else:
        n = - ( - n + conf.freq_spacing // 2) // conf.freq_spacing
      freq = conf.freq_base + n * conf.freq_spacing
      return freq - vfo
    else:
      return tune
  def OnRightDown(self, event):
    sample_rate = int(self.sample_rate * self.zoom)
    VFO = self.VFO + self.zoom_deltaf
    mouse_x, mouse_y = self.GetMousePosition(event)
    freq = float(mouse_x - self.x0) * sample_rate / self.data_width
    freq = int(freq)
    if VFO > 0:
      vfo = VFO + freq - self.zoom_deltaf
      if sample_rate > 40000:
        vfo = (vfo + 5000) // 10000 * 10000	# round to even number
      elif sample_rate > 5000:
        vfo = (vfo + 500) // 1000 * 1000
      else:
        vfo = (vfo + 50) // 100 * 100
      tune = freq + VFO - vfo
      tune = self.FreqRound(tune, vfo)
      self.ChangeHwFrequency(tune, vfo, 'MouseBtn3', event)
  def OnLeftDown(self, event):
    sample_rate = int(self.sample_rate * self.zoom)
    mouse_x, mouse_y = self.GetMousePosition(event)
    self.mouse_x = mouse_x
    x = mouse_x - self.originX
    if self.display.tune_rx and abs(x - self.display.tune_tx) > abs(x - self.display.tune_rx):
      self.mouse_is_rx = True
    else:
      self.mouse_is_rx = False
    if mouse_y < self.originY:		# click above X axis
      freq = float(mouse_x - self.x0) * sample_rate / self.data_width + self.zoom_deltaf
      freq = int(freq)
      if self.mouse_is_rx:
        application.rxFreq = freq
        application.screen.SetTxFreq(self.txFreq, freq)
        QS.set_tune(freq + application.ritFreq, self.txFreq)
      else:
        freq = self.FreqRound(freq, self.VFO)
        self.ChangeHwFrequency(freq, self.VFO, 'MouseBtn1', event)
    self.CaptureMouse()
  def OnLeftUp(self, event):
    if self.HasCapture():
      self.ReleaseMouse()
      freq = self.FreqRound(self.txFreq, self.VFO)
      if freq != self.txFreq:
        self.ChangeHwFrequency(freq, self.VFO, 'MouseMotion', event)
  def OnMotion(self, event):
    sample_rate = int(self.sample_rate * self.zoom)
    if event.Dragging() and event.LeftIsDown():
      mouse_x, mouse_y = self.GetMousePosition(event)
      if conf.mouse_tune_method:		# Mouse motion changes the VFO frequency
        x = (mouse_x - self.mouse_x)	# Thanks to VK6JBL
        self.mouse_x = mouse_x
        freq = float(x) * sample_rate / self.data_width
        freq = int(freq)
        self.ChangeHwFrequency(self.txFreq, self.VFO - freq, 'MouseMotion', event)
      else:		# Mouse motion changes the tuning frequency
        # Frequency changes more rapidly for higher mouse Y position
        speed = max(10, self.originY - mouse_y) / float(self.originY)
        x = (mouse_x - self.mouse_x)
        self.mouse_x = mouse_x
        freq = speed * x * sample_rate / self.data_width
        freq = int(freq)
        if self.mouse_is_rx:	# Mouse motion changes the receive frequency
          application.rxFreq += freq
          application.screen.SetTxFreq(self.txFreq, application.rxFreq)
          QS.set_tune(application.rxFreq + application.ritFreq, self.txFreq)
        else:					# Mouse motion changes the transmit frequency
          self.ChangeHwFrequency(self.txFreq + freq, self.VFO, 'MouseMotion', event)
  def OnWheel(self, event):
    if conf.freq_spacing:
      wm = conf.freq_spacing
    else:
      wm = self.WheelMod		# Round frequency when using mouse wheel
    mouse_x, mouse_y = self.GetMousePosition(event)
    x = mouse_x - self.originX
    if self.display.tune_rx and abs(x - self.display.tune_tx) > abs(x - self.display.tune_rx):
      freq = application.rxFreq + self.VFO + wm * event.GetWheelRotation() // event.GetWheelDelta()
      if conf.freq_spacing:
        freq = self.FreqRound(freq, 0)
      elif freq >= 0:
        freq = freq // wm * wm
      else:		# freq can be negative when the VFO is zero
        freq = - (- freq // wm * wm)
      tune = freq - self.VFO
      application.rxFreq = tune
      application.screen.SetTxFreq(self.txFreq, tune)
      QS.set_tune(tune + application.ritFreq, self.txFreq)
    else:
      freq = self.txFreq + self.VFO + wm * event.GetWheelRotation() // event.GetWheelDelta()
      if conf.freq_spacing:
        freq = self.FreqRound(freq, 0)
      elif freq >= 0:
        freq = freq // wm * wm
      else:		# freq can be negative when the VFO is zero
        freq = - (- freq // wm * wm)
      tune = freq - self.VFO
      self.ChangeHwFrequency(tune, self.VFO, 'MouseWheel', event)
  def ChangeHwFrequency(self, tune, vfo, source, event):
    application.ChangeHwFrequency(tune, vfo, source, event)
  def PeakHold(self, name):
    if name == 'GraphP1':
      self.display.peak_hold = int(self.display.scale * conf.graph_peak_hold_1)
    elif name == 'GraphP2':
      self.display.peak_hold = int(self.display.scale * conf.graph_peak_hold_2)
    else:
      self.display.peak_hold = 9999
    if self.display.peak_hold < 1:
      self.display.peak_hold = 1

class StationScreen(wx.Window):		# This code was contributed by Christof, DJ4CM.  Many Thanks!!
  """Create a window below the graph X axis to display interesting frequencies."""
  def __init__(self, parent, width, lines):
    self.lineMargin = 2
    self.lines = lines
    self.mouse_x = 0
    self.stationList = []
    graph = self.graph = application.graph
    height = lines * (graph.GetCharHeight() + self.lineMargin)	# The height may be zero
    wx.Window.__init__(self, parent, size=(graph.width, height), style = wx.NO_BORDER)
    self.font = wx.Font(conf.graph_font_size, wx.FONTFAMILY_SWISS, wx.NORMAL,
          wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface)
    self.SetFont(self.font)
    self.SetBackgroundColour(conf.color_graph)
    self.width = application.screen_width
    self.Bind(wx.EVT_PAINT, self.OnPaint)
    if lines:
      self.Bind(wx.EVT_LEFT_DOWN, self.OnLeftDown)
      self.Bind(wx.EVT_MOTION, self.OnMotion)
      self.Bind(wx.EVT_LEAVE_WINDOW, self.OnLeaveWindow)
      # handle station info
      self.stationWindow = wx.PopupWindow (parent)
      self.stationInfo = wx.richtext.RichTextCtrl(self.stationWindow)
      self.stationInfo.SetFont(wx.Font(conf.status_font_size, wx.FONTFAMILY_SWISS, wx.NORMAL,
          wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface))
      self.stationWindow.Hide(); 
      self.firstStationInRange = None
      self.lastStationX = 0
      self.nrStationInRange = 0
      self.tunedStation = 0
  def OnPaint(self, event):
    dc = wx.PaintDC(self)
    if not self.lines:
      return
    dc.SetFont(self.font)
    graph = self.graph
    dc.SetTextForeground(conf.color_graphlabels)
    dc.SetPen(graph.pen_tick)
    originX = graph.originX
    originY = graph.originY
    endX = originX + graph.graph_width    # end of fft data
    sample_rate = int(graph.sample_rate * graph.zoom)
    VFO = graph.VFO + graph.zoom_deltaf
    hl = self.GetCharHeight()
    y = 0
    for i in range (self.lines):
      dc.DrawLine(originX, y, endX, y)
      y += hl + self.lineMargin
    # create a sorted list of favorites in the frequency range  
    freq1 = VFO - sample_rate // 2
    freq2 = VFO + sample_rate // 2
    self.stationList = []
    fav = application.config_screen.favorites
    for row in range (fav.GetNumberRows()):
      fav_f = fav.GetCellValue(row, 1) 
      if fav_f:
        try:
          fav_f = str2freq(fav_f)
          if freq1 < fav_f < freq2:
            self.stationList.append((fav_f, conf.Xsym_stat_fav, fav.GetCellValue(row, 0),
                fav.GetCellValue(row, 2), fav.GetCellValue(row, 3)))
        except ValueError:
          pass            
    # add memory stations
    for mem_f, mem_band, mem_vfo, mem_txfreq, mem_mode in application.memoryState:
      if freq1 < mem_f < freq2:
        self.stationList.append((mem_f, conf.Xsym_stat_mem, '', mem_mode, ''))
    #add dx spots
    if application.dxCluster:
      for entry in application.dxCluster.dxSpots:
        if freq1 < entry.getFreq() < freq2:
          for i in range (0, entry.getLen()):
            descr = entry.getSpotter(i) + '\t' + entry.getTime(i) + '\t' + entry.getLocation(i) + '\n' + entry.getComment(i)
            if i < entry.getLen()-1:
              descr += '\n'
          self.stationList.append((entry.freq, conf.Xsym_stat_dx, entry.dx, '', descr))           
    # draw stations on graph
    self.stationList.sort(cmp=None, key=None, reverse=False)
    lastX = []
    line = 0
    for i in range (0, self.lines):
      lastX.append(graph.width)
    for statFreq, symbol, statName, statMode, statDscr in reversed (self.stationList):
      ws = dc.GetTextExtent(symbol)[0]
      statX = graph.x0 + int(float(statFreq - VFO) / sample_rate * graph.data_width)
      w, h = dc.GetTextExtent(statName)
      # shorten name until it fits into remaining space
      maxLen = 25
      tName = statName 
      while (w > lastX[line] - statX - ws - 4) and maxLen > 0:
        maxLen -= 1
        tName = statName[:maxLen] + '..'
        w, h = dc.GetTextExtent(tName)
      dc.DrawLine(statX, line * (hl+self.lineMargin), statX, line * (hl+self.lineMargin) + 4)                    
      dc.DrawText(symbol + ' ' + tName, statX - ws//2, line * (hl+self.lineMargin) + self.lineMargin//2+1)
      lastX[line] = statX
      line = (line+1)%self.lines
  def OnLeftDown(self, event):
    if self.firstStationInRange != None:
      # tune to station
      if self.tunedStation >= self.nrStationInRange:
        self.tunedStation = 0      
      freq, symbol, name, mode, dscr = self.stationList[self.firstStationInRange+self.tunedStation]
      self.tunedStation += 1
      if mode != '': # information about mode available
        mode = mode.upper()
        application.OnBtnMode(None, mode)
      application.ChangeRxTxFrequency(None, freq)
  def OnMotion(self, event):
    mouse_x, mouse_y = event.GetPositionTuple()
    x = (mouse_x - self.mouse_x)
    application.isTuning = False
    # show detailed station info
    if abs(self.lastStationX - mouse_x) > 30:
      self.firstStationInRange = None   
    found = False
    graph = self.graph
    sample_rate = int(graph.sample_rate * graph.zoom)
    VFO = graph.VFO + graph.zoom_deltaf
    if abs(x) > 5: # ignore small mouse moves
      for index in range (0, len(self.stationList)):
        statFreq, symbol, statName, statMode, statDscr = self.stationList[index]
        statX = graph.x0 + int(float(statFreq - VFO) / sample_rate * graph.data_width)
        if abs(mouse_x-statX) < 10: 
          self.lastStationX = mouse_x
          if found == False:
            self.firstStationInRange = index
            self.nrStationInRange = 0
            self.stationInfo.Clear()
            found = True
          self.nrStationInRange += 1
          attr = self.stationInfo.GetBasicStyle()
          attr.SetFlags(wx.TEXT_ATTR_TABS)
          attr.SetTabs((40, 400, 700))
          self.stationInfo.SetBasicStyle(attr)
          self.stationInfo.BeginSymbolBullet(symbol, 0, 40) 
          self.stationInfo.BeginBold()      
          self.stationInfo.WriteText(statName + '\t')
          self.stationInfo.EndBold()
          self.stationInfo.WriteText (str(statFreq) + ' Hz\t' + statMode)
          self.stationInfo.Newline()
          self.stationInfo.EndSymbolBullet() 
          self.stationInfo.BeginLeftIndent(40)
          if len(statDscr) > 0:
            self.stationInfo.WriteText(statDscr)
            self.stationInfo.Newline()
          self.stationInfo.EndLeftIndent()   
          self.mouse_x = mouse_x   
    if self.firstStationInRange != None:
      line = self.stationInfo.GetVisibleLineForCaretPosition(self.stationInfo.GetCaretPosition()) 
      cy = line.GetAbsolutePosition()[1]
      self.stationWindow.SetClientSize((340, cy+2))
      self.stationInfo.SetClientSize((340, cy+2))
      # convert coordinates to screen
      sx, sy = self.ClientToScreenXY(mouse_x, mouse_y) 
      w, h = self.stationInfo.GetClientSize()
      self.stationWindow.Move((sx - w * sx//graph.width, sy - h - 4)) 
      if not self.stationWindow.IsShown():
        self.stationWindow.Show()
    else:
      self.stationWindow.Hide()
  def OnLeaveWindow(self, event):
    self.stationWindow.Hide() 
                              

class WaterfallDisplay(wx.Window):
  """Create a waterfall display within the waterfall screen."""
  def __init__(self, parent, x, y, graph_width, height, margin):
    wx.Window.__init__(self, parent,
       pos = (x, y),
       size = (graph_width, height),
       style = wx.NO_BORDER)
    self.parent = parent
    self.graph_width = graph_width
    self.margin = margin
    self.height = 10
    self.zoom = 1.0
    self.zoom_deltaf = 0
    self.rf_gain = 0	# Keep waterfall colors constant for variable RF gain
    self.sample_rate = application.sample_rate
    self.SetBackgroundColour('Black')
    self.Bind(wx.EVT_PAINT, self.OnPaint)
    self.Bind(wx.EVT_LEFT_DOWN, parent.OnLeftDown)
    self.Bind(wx.EVT_RIGHT_DOWN, parent.OnRightDown)
    self.Bind(wx.EVT_LEFT_UP, parent.OnLeftUp)
    self.Bind(wx.EVT_MOTION, parent.OnMotion)
    self.Bind(wx.EVT_MOUSEWHEEL, parent.OnWheel)
    self.tune_tx = graph_width // 2	# Current X position of the Tx tuning line
    self.tune_rx = 0				# Current X position of Rx tuning line or zero
    self.tuningPen = wx.Pen('White', 3)
    self.marginPen = wx.Pen(conf.color_graph, 1)
    # Size of top faster scroll region is (top_key + 2) * (top_key - 1) // 2
    self.top_key = 8
    self.top_size = (self.top_key + 2) * (self.top_key - 1) // 2
    # Make the palette
    pal2 = conf.waterfallPalette
    red = []
    green = []
    blue = []
    n = 0
    for i in range(256):
      if i > pal2[n+1][0]:
         n = n + 1
      red.append((i - pal2[n][0]) *
       (pal2[n+1][1] - pal2[n][1]) //
       (pal2[n+1][0] - pal2[n][0]) + pal2[n][1])
      green.append((i - pal2[n][0]) *
       (pal2[n+1][2] - pal2[n][2]) //
       (pal2[n+1][0] - pal2[n][0]) + pal2[n][2])
      blue.append((i - pal2[n][0]) *
       (pal2[n+1][3] - pal2[n][3]) //
       (pal2[n+1][0] - pal2[n][0]) + pal2[n][3])
    self.red = red
    self.green = green
    self.blue = blue
    bmp = wx.EmptyBitmap(0, 0)
    bmp.x_origin = 0
    self.bitmaps = [bmp] * application.screen_height
    if sys.platform == 'win32':
      self.Bind(wx.EVT_ENTER_WINDOW, self.OnEnter)
  def OnEnter(self, event):
    if not application.w_phase:
      self.SetFocus()	# Set focus so we get mouse wheel events
  def OnPaint(self, event):
    sample_rate = int(self.sample_rate * self.zoom)
    dc = wx.BufferedPaintDC(self)
    dc.SetTextForeground(conf.color_graphlabels)
    dc.SetBackground(wx.Brush('Black'))
    dc.Clear()
    y = 0
    dc.SetPen(self.marginPen)
    x_origin = int(float(self.VFO) / sample_rate * self.data_width + 0.5)
    for i in range(0, self.margin):
      dc.DrawLine(0, y, self.graph_width, y)
      y += 1
    index = 0
    if conf.waterfall_scroll_mode:	# Draw the first few lines multiple times
      for i in range(self.top_key, 1, -1):
        b = self.bitmaps[index]
        x = b.x_origin - x_origin
        for j in range(0, i):
          dc.DrawBitmap(b, x, y)
          y += 1
        index += 1
    while y < self.height:
      b = self.bitmaps[index]
      x = b.x_origin - x_origin
      dc.DrawBitmap(b, x, y)
      y += 1
      index += 1
    dc.SetPen(self.tuningPen)
    dc.SetLogicalFunction(wx.XOR)
    dc.DrawLine(self.tune_tx, 0, self.tune_tx, self.height)
    if self.tune_rx:
      dc.DrawLine(self.tune_rx, 0, self.tune_rx, self.height)
  def SetHeight(self, height):
    self.height = height
    self.SetSize((self.graph_width, height))
  def OnGraphData(self, data, y_zero, y_scale):
    sample_rate = int(self.sample_rate * self.zoom)
    #T('graph start')
    row = ''		# Make a new row of pixels for a one-line image
    gain = self.rf_gain
    for x in data:	# x is -130 to 0, or so (dB)
      l = int((x - gain + y_zero // 3 + 100) * y_scale / 10)
      l = max(l, 0)
      l = min(l, 255)
      row = row + "%c%c%c" % (chr(self.red[l]), chr(self.green[l]), chr(self.blue[l]))
    #T('graph string')
    # Perhaps use 4-byte pixels and BitmapFromBufferRGBA()
    bmp = wx.BitmapFromBuffer(len(row) // 3, 1, row)
    bmp.x_origin = int(float(self.VFO) / sample_rate * self.data_width + 0.5)
    self.bitmaps.insert(0, bmp)
    del self.bitmaps[-1]
    #self.ScrollWindow(0, 1, None)
    #self.Refresh(False, (0, 0, self.graph_width, self.top_size + self.margin))
    self.Refresh(False)
    #T('graph end')
  def SetTuningLine(self, tune_tx, tune_rx):
    dc = wx.ClientDC(self)
    dc.SetPen(self.tuningPen)
    dc.SetLogicalFunction(wx.XOR)
    dc.DrawLine(self.tune_tx, 0, self.tune_tx, self.height)
    if self.tune_rx:
      dc.DrawLine(self.tune_rx, 0, self.tune_rx, self.height)
    dc.DrawLine(tune_tx, 0, tune_tx, self.height)
    if tune_rx:
      dc.DrawLine(tune_rx, 0, tune_rx, self.height)
    self.tune_tx = tune_tx
    self.tune_rx = tune_rx
  def ChangeZoom(self, zoom, deltaf):
    self.zoom = zoom
    self.zoom_deltaf = deltaf

class WaterfallScreen(wx.SplitterWindow):
  """Create a splitter window with a graph screen and a waterfall screen"""
  def __init__(self, frame, width, data_width, graph_width):
    self.y_scale = conf.waterfall_y_scale
    self.y_zero = conf.waterfall_y_zero
    wx.SplitterWindow.__init__(self, frame)
    self.SetSizeHints(width, -1, width)
    self.SetMinimumPaneSize(1)
    self.SetSize((width, conf.waterfall_graph_size + 100))	# be able to set sash size
    self.pane1 = GraphScreen(self, data_width, graph_width, 1)
    self.pane2 = WaterfallPane(self, data_width, graph_width)
    self.SplitHorizontally(self.pane1, self.pane2, conf.waterfall_graph_size)
  def OnIdle(self, event):
    self.pane1.OnIdle(event)
    self.pane2.OnIdle(event)
  def SetTxFreq(self, tx_freq, rx_freq):
    self.pane1.SetTxFreq(tx_freq, rx_freq)
    self.pane2.SetTxFreq(tx_freq, rx_freq)
  def SetVFO(self, vfo):
    self.pane1.SetVFO(vfo)
    self.pane2.SetVFO(vfo) 
  def ChangeYscale(self, y_scale):		# Test if the shift key is down
    if wx.GetKeyState(wx.WXK_SHIFT):	# Set graph screen
      self.pane1.ChangeYscale(y_scale)
    else:			# Set waterfall screen
      self.y_scale = y_scale
      self.pane2.ChangeYscale(y_scale)
  def ChangeYzero(self, y_zero):		# Test if the shift key is down
    if wx.GetKeyState(wx.WXK_SHIFT):	# Set graph screen
      self.pane1.ChangeYzero(y_zero)
    else:			# Set waterfall screen
      self.y_zero = y_zero
      self.pane2.ChangeYzero(y_zero)
  def SetPane2(self, ysz):
    y_scale, y_zero = ysz
    self.y_scale = y_scale
    self.pane2.ChangeYscale(y_scale)
    self.y_zero = y_zero
    self.pane2.ChangeYzero(y_zero)
  def OnGraphData(self, data):
    self.pane1.OnGraphData(data)
    self.pane2.OnGraphData(data)
  def ChangeRfGain(self, gain):		# Set the correction for RF gain
    self.pane2.display.rf_gain = gain

class WaterfallPane(GraphScreen):
  """Create a waterfall screen with an X axis and a waterfall display."""
  def __init__(self, frame, data_width, graph_width):
    GraphScreen.__init__(self, frame, data_width, graph_width)
    self.y_scale = conf.waterfall_y_scale
    self.y_zero = conf.waterfall_y_zero
    self.oldVFO = self.VFO
  def MakeDisplay(self):
    self.display = WaterfallDisplay(self, self.originX, 0, self.graph_width, 5, self.chary)
    self.display.VFO = self.VFO
    self.display.data_width = self.data_width
  def SetVFO(self, vfo):
    GraphScreen.SetVFO(self, vfo)
    self.display.VFO = vfo
    if self.oldVFO != vfo:
      self.oldVFO = vfo
      self.Refresh()
  def MakeYTicks(self, dc):
    pass
  def ChangeYscale(self, y_scale):
    self.y_scale = y_scale
  def ChangeYzero(self, y_zero):
    self.y_zero = y_zero
  def OnGraphData(self, data):
    i1 = (self.data_width - self.graph_width) // 2
    i2 = i1 + self.graph_width
    self.display.OnGraphData(data[i1:i2], self.y_zero, self.y_scale)

class ScopeScreen(wx.Window):
  """Create an oscilloscope screen (mostly used for debug)."""
  def __init__(self, parent, width, data_width, graph_width):
    wx.Window.__init__(self, parent, pos = (0, 0),
       size=(width, -1), style = wx.NO_BORDER)
    self.SetBackgroundColour(conf.color_graph)
    self.font = wx.Font(conf.config_font_size, wx.FONTFAMILY_SWISS, wx.NORMAL,
          wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface)
    self.SetFont(self.font)
    self.Bind(wx.EVT_SIZE, self.OnSize)
    self.Bind(wx.EVT_PAINT, self.OnPaint)
    self.horizPen = wx.Pen(conf.color_gl, 1, wx.SOLID)
    self.y_scale = conf.scope_y_scale
    self.y_zero = conf.scope_y_zero
    self.yscale = 1
    self.running = 1
    self.doResize = False
    self.width = width
    self.height = 100
    self.originY = self.height // 2
    self.data_width = data_width
    self.graph_width = graph_width
    w = self.charx = self.GetCharWidth()
    h = self.chary = self.GetCharHeight()
    tick = max(2, h * 3 // 10)
    self.originX = w * 3
    self.width = self.originX + self.graph_width + tick + self.charx * 2
    self.line = [(0,0), (1,1)]	# initial fake graph data
    self.fpout = None #open("jim96.txt", "w")
  def OnIdle(self, event):
    if self.doResize:
      self.ResizeGraph()
  def OnSize(self, event):
    self.doResize = True
    event.Skip()
  def ResizeGraph(self, event=None):
    # Change the height of the graph.  Changing the width interactively is not allowed.
    w, h = self.GetClientSize()
    self.height = h
    self.originY = h // 2
    self.doResize = False
    self.Refresh()
  def OnPaint(self, event):
    dc = wx.PaintDC(self)
    dc.SetFont(self.font)
    dc.SetTextForeground(conf.color_graphlabels)
    self.MakeYTicks(dc)
    self.MakeXTicks(dc)
    self.MakeText(dc)
    dc.SetPen(wx.Pen(conf.color_graphline, 1))
    dc.DrawLines(self.line)
  def MakeYTicks(self, dc):
    chary = self.chary
    originX = self.originX
    x3 = self.x3 = originX + self.graph_width	# end of graph data
    dc.SetPen(wx.Pen(conf.color_graphticks,1))
    dc.DrawLine(originX, 0, originX, self.originY * 3)	# y axis
    # Find the size of the Y scale markings
    themax = 2.5e9 * 10.0 ** - ((160 - self.y_scale) / 50.0)	# value at top of screen
    themax = int(themax)
    l = []
    for j in (5, 6, 7, 8):
      for i in (1, 2, 5):
        l.append(i * 10 ** j)
    for yvalue in l:
      n = themax // yvalue + 1			# Number of lines
      ypixels = self.height // n
      if n < 20:
        break
    dc.SetPen(self.horizPen)
    for i in range(1, 1000):
      y = self.originY - ypixels * i
      if y < chary:
        break
      # Above axis
      dc.DrawLine(originX, y, x3, y)	# y line
      # Below axis
      y = self.originY + ypixels * i
      dc.DrawLine(originX, y, x3, y)	# y line
    self.yscale = float(ypixels) / yvalue
    self.yvalue = yvalue
  def MakeXTicks(self, dc):
    originY = self.originY
    x3 = self.x3
    # Draw the X axis
    dc.SetPen(wx.Pen(conf.color_graphticks,1))
    dc.DrawLine(self.originX, originY, x3, originY)
    # Find the size of the X scale markings in microseconds
    for i in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000):
      xscale = i			# X scale in microseconds
      if application.sample_rate * xscale * 0.000001 > self.width // 30:
        break
    # Draw the X lines
    dc.SetPen(self.horizPen)
    for i in range(1, 999):
      x = int(self.originX + application.sample_rate * xscale * 0.000001 * i + 0.5)
      if x > x3:
        break
      dc.DrawLine(x, 0, x, self.height)	# x line
    self.xscale = xscale
  def MakeText(self, dc):
    if self.running:
      t = "   RUN"
    else:
      t = "   STOP"
    if self.xscale >= 1000:
      t = "%s    X: %d millisec/div" % (t, self.xscale // 1000)
    else:
      t = "%s    X: %d microsec/div" % (t, self.xscale)
    t = "%s   Y: %.0E/div" % (t, self.yvalue)
    dc.DrawText(t, self.originX, self.height - self.chary)
  def OnGraphData(self, data):
    if not self.running:
      if self.fpout:
        for cpx in data:
          re = int(cpx.real)
          im = int(cpx.imag)
          ab = int(abs(cpx))
          ph = math.atan2(im, re) * 360. / (2.0 * math.pi)
          self.fpout.write("%12d %12d %12d %12.1d\n" % (re, im, ab, ph))
      return		# Preserve data on screen
    line = []
    x = self.originX
    ymax = self.height
    for cpx in data:	# cpx is complex raw samples +/- 0 to 2**31-1
      y = cpx.real
      #y = abs(cpx)
      y = self.originY - int(y * self.yscale + 0.5)
      if y > ymax:
        y = ymax
      elif y < 0:
        y = 0
      line.append((x, y))
      x = x + 1
    self.line = line
    self.Refresh()
  def ChangeYscale(self, y_scale):
    self.y_scale = y_scale
    self.doResize = True
  def ChangeYzero(self, y_zero):
    self.y_zero = y_zero
  def SetTxFreq(self, tx_freq, rx_freq):
    pass

class FilterScreen(GraphScreen):
  """Create a graph of the receive filter response."""
  def __init__(self, parent, data_width, graph_width):
    GraphScreen.__init__(self, parent, data_width, graph_width)
    self.y_scale = conf.filter_y_scale
    self.y_zero = conf.filter_y_zero
    self.VFO = 0
    self.txFreq = 0
    self.data = []
    self.sample_rate = QS.get_filter_rate()
  def NewFilter(self):
    self.sample_rate = QS.get_filter_rate()
    self.data = QS.get_filter()
    mx = -1000
    for x in self.data:
      if mx < x:
        mx = x
    mx -= 3.0
    f1 = None
    for i in range(len(self.data)):
      x = self.data[i]
      if x > mx:
        if f1 is None:
          f1 = i
        f2 = i
    bw3 = float(f2 - f1) / len(self.data) * self.sample_rate
    mx -= 3.0
    f1 = None
    for i in range(len(self.data)):
      x = self.data[i]
      if x > mx:
        if f1 is None:
          f1 = i
        f2 = i
    bw6 = float(f2 - f1) / len(self.data) * self.sample_rate
    self.display.display_text = "Filter 3 dB bandwidth %.0f, 6 dB %.0f" % (bw3, bw6)
    #self.data = QS.get_tx_filter()
    self.doResize = True
  def OnGraphData(self, data):
    GraphScreen.OnGraphData(self, self.data)
  def ChangeHwFrequency(self, tune, vfo, source, event):
    GraphScreen.SetTxFreq(self, tune, tune)
    application.freqDisplay.Display(tune)
  def SetTxFreq(self, tx_freq, rx_freq):
    pass

class HelpScreen(wx.html.HtmlWindow):
  """Create the screen for the Help button."""
  def __init__(self, parent, width, height):
    wx.html.HtmlWindow.__init__(self, parent, -1, size=(width, height))
    self.y_scale = 0
    self.y_zero = 0
    if "gtk2" in wx.PlatformInfo:
      self.SetStandardFonts()
    self.SetFonts("", "", [10, 12, 14, 16, 18, 20, 22])
    # read in text from file help.html in the directory of this module
    self.LoadFile('help.html')
  def OnGraphData(self, data):
    pass
  def ChangeYscale(self, y_scale):
    pass
  def ChangeYzero(self, y_zero):
    pass
  def OnIdle(self, event):
    pass
  def SetTxFreq(self, tx_freq, rx_freq):
    pass
  def OnLinkClicked(self, link):
    webbrowser.open(link.GetHref(), new=2)

class QMainFrame(wx.Frame):
  """Create the main top-level window."""
  def __init__(self, width, height):
    fp = open('__init__.py')		# Read in the title
    title = fp.readline().strip()[1:]
    fp.close()
    wx.Frame.__init__(self, None, -1, title, wx.DefaultPosition,
        (width, height), wx.DEFAULT_FRAME_STYLE, 'MainFrame')
    self.SetBackgroundColour(conf.color_bg)
    self.SetForegroundColour(conf.color_bg_txt)
    self.Bind(wx.EVT_CLOSE, self.OnBtnClose)
  def OnBtnClose(self, event):
    application.OnBtnClose(event)
    self.Destroy()

## Note: The new amplitude/phase adjustments have ideas provided by Andrew Nilsson, VK6JBL
class QAdjustPhase(wx.Frame):
  """Create a window with amplitude and phase adjustment controls"""
  f_ampl = "Amplitude adjustment %.6f"
  f_phase = "Phase adjustment degrees %.6f"
  def __init__(self, parent, width, rx_tx):
    self.rx_tx = rx_tx		# Must be "rx" or "tx"
    if rx_tx == 'tx':
      self.is_tx = 1
      t = "Adjust Sound Card Transmit Amplitude and Phase"
    else:
      self.is_tx = 0
      t = "Adjust Sound Card Receive Amplitude and Phase"
    wx.Frame.__init__(self, application.main_frame, -1, t, pos=(50, 100), style=wx.CAPTION)
    panel = wx.Panel(self)
    self.MakeControls(panel, width)
    self.Show()
  def MakeControls(self, panel, width):		# Make controls for phase/amplitude adjustment
    self.old_amplitude, self.old_phase = application.GetAmplPhase(self.is_tx)
    self.new_amplitude, self.new_phase = self.old_amplitude, self.old_phase
    sl_max = width * 4 // 10		# maximum +/- value for slider
    self.ampl_scale = float(conf.rx_max_amplitude_correct) / sl_max
    self.phase_scale = float(conf.rx_max_phase_correct) / sl_max
    font = wx.Font(conf.default_font_size, wx.FONTFAMILY_SWISS, wx.NORMAL,
          wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface)
    chary = self.GetCharHeight()
    y = chary * 3 // 10
    # Print available data points
    if "panadapter" in conf.bandAmplPhase:
      self.band = "panadapter"
    else:
      self.band = application.lastBand
    app_vfo = (application.VFO + 500) // 1000
    ap = application.bandAmplPhase
    if self.band not in ap:
      ap[self.band] = {}
    if self.rx_tx not in ap[self.band]:
      ap[self.band][self.rx_tx] = []
    lst = ap[self.band][self.rx_tx]
    freq_in_list = False
    if lst:
      t = "Band %s: VFO" % self.band
      for l in lst:
        vfo = (l[0] + 500) // 1000
        if vfo == app_vfo:
          freq_in_list = True
        t = t + (" %d" % vfo)
    else:
      t = "Band %s: No data." % self.band
    txt = wx.StaticText(panel, -1, t, pos=(0, y))
    txt.SetFont(font)
    y += txt.GetSizeTuple()[1]
    self.t_ampl = wx.StaticText(panel, -1, self.f_ampl % self.old_amplitude, pos=(0, y))
    self.t_ampl.SetFont(font)
    y += self.t_ampl.GetSizeTuple()[1]
    self.ampl1 = wx.Slider(panel, -1, 0, -sl_max, sl_max,
      pos=(0, y), size=(width, -1))
    y += self.ampl1.GetSizeTuple()[1]
    self.ampl2 = wx.Slider(panel, -1, 0, -sl_max, sl_max,
      pos=(0, y), size=(width, -1))
    y += self.ampl2.GetSizeTuple()[1]
    self.PosAmpl(self.old_amplitude)
    self.t_phase = wx.StaticText(panel, -1, self.f_phase % self.old_phase, pos=(0, y))
    self.t_phase.SetFont(font)
    y += self.t_phase.GetSizeTuple()[1]
    self.phase1 = wx.Slider(panel, -1, 0, -sl_max, sl_max,
      pos=(0, y), size=(width, -1))
    y += self.phase1.GetSizeTuple()[1]
    self.phase2 = wx.Slider(panel, -1, 0, -sl_max, sl_max,
      pos=(0, y), size=(width, -1))
    y += self.phase2.GetSizeTuple()[1]
    sv = QuiskPushbutton(panel, self.OnBtnSave, 'Save %d' % app_vfo)
    ds = QuiskPushbutton(panel, self.OnBtnDiscard, 'Destroy %d' % app_vfo)
    cn = QuiskPushbutton(panel, self.OnBtnCancel, 'Cancel')
    w, h = ds.GetSizeTuple()
    sv.SetSize((w, h))
    cn.SetSize((w, h))
    y += h // 4
    x = (width - w * 3) // 4
    sv.SetPosition((x, y))
    ds.SetPosition((x*2 + w, y))
    cn.SetPosition((x*3 + w*2, y))
    sv.SetBackgroundColour('light blue')
    ds.SetBackgroundColour('light blue')
    cn.SetBackgroundColour('light blue')
    if not freq_in_list:
      ds.Disable()
    y += h
    y += h // 4
    self.ampl1.SetBackgroundColour('aquamarine')
    self.ampl2.SetBackgroundColour('orange')
    self.phase1.SetBackgroundColour('aquamarine')
    self.phase2.SetBackgroundColour('orange')
    self.PosPhase(self.old_phase)
    self.SetClientSizeWH(width, y)
    self.ampl1.Bind(wx.EVT_SCROLL, self.OnChange)
    self.ampl2.Bind(wx.EVT_SCROLL, self.OnAmpl2)
    self.phase1.Bind(wx.EVT_SCROLL, self.OnChange)
    self.phase2.Bind(wx.EVT_SCROLL, self.OnPhase2)
  def PosAmpl(self, ampl):	# set pos1, pos2 for amplitude
    pos2 = round(ampl / self.ampl_scale)
    remain = ampl - pos2 * self.ampl_scale
    pos1 = round(remain / self.ampl_scale * 50.0)
    self.ampl1.SetValue(pos1)
    self.ampl2.SetValue(pos2)
  def PosPhase(self, phase):	# set pos1, pos2 for phase
    pos2 = round(phase / self.phase_scale)
    remain = phase - pos2 * self.phase_scale
    pos1 = round(remain / self.phase_scale * 50.0)
    self.phase1.SetValue(pos1)
    self.phase2.SetValue(pos2)
  def OnChange(self, event):
    ampl = self.ampl_scale * self.ampl1.GetValue() / 50.0 + self.ampl_scale * self.ampl2.GetValue()
    if abs(ampl) < self.ampl_scale * 3.0 / 50.0:
      ampl = 0.0
    self.t_ampl.SetLabel(self.f_ampl % ampl)
    phase = self.phase_scale * self.phase1.GetValue() / 50.0 + self.phase_scale * self.phase2.GetValue()
    if abs(phase) < self.phase_scale * 3.0 / 50.0:
      phase = 0.0
    self.t_phase.SetLabel(self.f_phase % phase)
    QS.set_ampl_phase(ampl, phase, self.is_tx)
    self.new_amplitude, self.new_phase = ampl, phase
  def OnAmpl2(self, event):		# re-center the fine slider when the coarse slider is adjusted
    ampl = self.ampl_scale * self.ampl1.GetValue() / 50.0 + self.ampl_scale * self.ampl2.GetValue()
    self.PosAmpl(ampl)
    self.OnChange(event)
  def OnPhase2(self, event):	# re-center the fine slider when the coarse slider is adjusted
    phase = self.phase_scale * self.phase1.GetValue() / 50.0 + self.phase_scale * self.phase2.GetValue()
    self.PosPhase(phase)
    self.OnChange(event)
  def DeleteEqual(self):	# Remove entry with the same VFO
    ap = application.bandAmplPhase
    lst = ap[self.band][self.rx_tx]
    vfo = (application.VFO + 500) // 1000
    for i in range(len(lst)-1, -1, -1):
      if (lst[i][0] + 500) // 1000 == vfo:
        del lst[i]
  def OnBtnSave(self, event):
    data = (application.VFO, application.rxFreq, self.new_amplitude, self.new_phase)
    self.DeleteEqual()
    ap = application.bandAmplPhase
    lst = ap[self.band][self.rx_tx]
    lst.append(data)
    lst.sort()
    application.w_phase = None
    self.Destroy()
  def OnBtnDiscard(self, event):
    self.DeleteEqual()
    self.OnBtnCancel()
  def OnBtnCancel(self, event=None):
    QS.set_ampl_phase(self.old_amplitude, self.old_phase, self.is_tx)
    application.w_phase = None
    self.Destroy()

class Spacer(wx.Window):
  """Create a bar between the graph screen and the controls"""
  def __init__(self, parent):
    wx.Window.__init__(self, parent, pos = (0, 0),
       size=(-1, 6), style = wx.NO_BORDER)
    self.Bind(wx.EVT_PAINT, self.OnPaint)
    r, g, b = parent.GetBackgroundColour().Get()
    dark = (r * 7 // 10, g * 7 // 10, b * 7 // 10)
    light = (r + (255 - r) * 5 // 10, g + (255 - g) * 5 // 10, b + (255 - b) * 5 // 10)
    self.dark_pen = wx.Pen(dark, 1, wx.SOLID)
    self.light_pen = wx.Pen(light, 1, wx.SOLID)
    self.width = application.screen_width
  def OnPaint(self, event):
    dc = wx.PaintDC(self)
    w = self.width
    dc.SetPen(self.dark_pen)
    dc.DrawLine(0, 0, w, 0)
    dc.DrawLine(0, 1, w, 1)
    dc.DrawLine(0, 2, w, 2)
    dc.SetPen(self.light_pen)
    dc.DrawLine(0, 3, w, 3)
    dc.DrawLine(0, 4, w, 4)
    dc.DrawLine(0, 5, w, 5)

class App(wx.App):
  """Class representing the application."""
  StateNames = [		# Names of state attributes to save and restore
  'bandState', 'bandAmplPhase', 'lastBand', 'VFO', 'txFreq', 'mode',
  'vardecim_set', 'filterAdjBw1', 'levelAGC', 'levelOffAGC', 'volumeAudio', 'levelSpot',
  'levelSquelch', 'levelVOX', 'timeVOX',
  'txAudioClipUsb', 'txAudioClipAm','txAudioClipFm',
  'txAudioPreemphUsb', 'txAudioPreemphAm', 'txAudioPreemphFm',
  'wfallScaleZ']
  def __init__(self):
    global application
    application = self
    self.init_path = None
    self.bottom_widgets = None
    self.dxCluster = None
    if sys.stdout.isatty():
      wx.App.__init__(self, redirect=False)
    else:
      wx.App.__init__(self, redirect=True)
  def QuiskText(self, *args, **kw):			# Make our text control available to widget files
    return QuiskText(*args, **kw)
  def QuiskPushbutton(self, *args, **kw):	# Make our buttons available to widget files
    return QuiskPushbutton(*args, **kw)
  def  QuiskRepeatbutton(self, *args, **kw):
    return QuiskRepeatbutton(*args, **kw)
  def QuiskCheckbutton(self, *args, **kw):
    return QuiskCheckbutton(*args, **kw)
  def QuiskCycleCheckbutton(self, *args, **kw):
    return QuiskCycleCheckbutton(*args, **kw)
  def RadioButtonGroup(self, *args, **kw):
    return RadioButtonGroup(*args, **kw)
  def OnInit(self):
    """Perform most initialization of the app here (called by wxPython on startup)."""
    wx.lib.colourdb.updateColourDB()	# Add additional color names
    import quisk_widgets		# quisk_widgets needs the application object
    quisk_widgets.application = self
    del quisk_widgets
    global conf		# conf is the module for all configuration data
    import quisk_conf_defaults as conf
    setattr(conf, 'config_file_path', ConfigPath)
    if os.path.isfile(ConfigPath):	# See if the user has a config file
      setattr(conf, 'config_file_exists', True)
      d = {}
      d.update(conf.__dict__)		# make items from conf available
      exec(compile(open(ConfigPath).read(), ConfigPath, 'exec'), d)		# execute the user's config file
      if os.path.isfile(ConfigPath2):	# See if the user has a second config file
        exec(compile(open(ConfigPath2).read(), ConfigPath2, 'exec'), d)	# execute the user's second config file
      for k, v in d.items():		# add user's config items to conf
        if k[0] != '_':				# omit items starting with '_'
          setattr(conf, k, v)
    else:
      setattr(conf, 'config_file_exists', False)
    # Choose whether to use Unicode or text symbols
    for k in ('sym_stat_mem', 'sym_stat_fav', 'sym_stat_dx',
        'btn_text_range_dn', 'btn_text_range_up', 'btn_text_play', 'btn_text_rec', 'btn_text_file_rec', 'btn_text_fav_add',
        'btn_text_fav_recall', 'btn_text_mem_add', 'btn_text_mem_next', 'btn_text_mem_del'):
      if conf.use_unicode_symbols:
        setattr(conf, 'X' + k, getattr(conf, 'U' + k))
      else:
        setattr(conf, 'X' + k, getattr(conf, 'T' + k))
    if conf.invertSpectrum:
      QS.invert_spectrum(1)
    self.wfallScaleZ = {}		# scale and zero for the waterfall pane2
    self.bandState = {}			# for key band, the current (self.VFO, self.txFreq, self.mode)
    self.bandState.update(conf.bandState)
    self.memoryState = []		# a list of (freq, band, self.VFO, self.txFreq, self.mode)
    self.bandAmplPhase = conf.bandAmplPhase
    # Open hardware file
    global Hardware
    if hasattr(conf, "Hardware"):	# Hardware defined in config file
      Hardware = conf.Hardware(self, conf)
    else:
      Hardware = conf.quisk_hardware.Hardware(self, conf)
    self.Hardware = Hardware
    # Initialization - may be over-written by persistent state
    self.clip_time0 = 0		# timer to display a CLIP message on ADC overflow
    self.smeter_db_count = 0	# average the S-meter
    self.smeter_db_sum = 0
    self.smeter_db = 0
    self.smeter_avg_seconds = 1.0	# seconds for S-meter average
    self.smeter_sunits = -87.0
    self.smeter_usage = "smeter"	# specify use of s-meter display
    self.timer = time.time()		# A seconds clock
    self.heart_time0 = self.timer	# timer to call HeartBeat at intervals
    self.save_time0 = self.timer
    self.smeter_db_time0 = self.timer
    self.smeter_sunits_time0 = self.timer
    self.band_up_down = 0			# Are band Up/Down buttons in use?
    self.lastBand = 'Audio'
    self.filterAdjBw1 = 1000
    self.levelAGC = 500				# AGC level ON control, 0 to 1000
    self.levelOffAGC = 100			# AGC level OFF control, 0 to 1000
    self.use_AGC = True				# AGC is ON
    self.levelSquelch = 500			# FM squelch level, 0 to 1000
    self.use_squelch = True			# squelch is in use
    self.levelVOX = -20				# audio level that triggers VOX
    self.timeVOX = 500				# hang time for VOX
    self.useVOX = False				# Is the VOX button down?
    self.txAudioClipUsb = 5			# Tx audio clip level in dB
    self.txAudioClipAm = 0
    self.txAudioClipFm = 0
    self.txAudioPreemphUsb = 70		# Tx audio preemphasis 0 to 100
    self.txAudioPreemphAm = 0
    self.txAudioPreemphFm = 0
    self.levelSpot = 500			# Spot level control, 10 to 1000
    self.volumeAudio = 300			# audio volume
    self.VFO = 0					# frequency of the VFO
    self.ritFreq = 0				# receive incremental tuning frequency offset
    self.txFreq = 0				# Transmit frequency as +/- sample_rate/2
    self.rxFreq = 0				# Receive  frequency as +/- sample_rate/2
    self.tx_level = 100			# initially 100%
    self.digital_tx_level = conf.digital_tx_level
    self.hot_key_ptt_on = False
    self.fft_size = 1
    # Quisk control by Hamlib through rig 2
    self.hamlib_clients = []	# list of TCP connections to handle
    if conf.hamlib_port:
      try:
        self.hamlib_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.hamlib_socket.bind(('localhost', conf.hamlib_port))
        self.hamlib_socket.settimeout(0.0)
        self.hamlib_socket.listen(0)	# listen for TCP connections from multiple clients
      except:
        self.hamlib_socket = None
        # traceback.print_exc()
    else:
      self.hamlib_socket = None
    # Quisk control by fldigi
    self.fldigi_new_freq = None
    self.fldigi_freq = None
    if conf.digital_xmlrpc_url:
      self.fldigi_server = ServerProxy(conf.digital_xmlrpc_url)
    else:
      self.fldigi_server = None
    self.fldigi_rxtx = 'rx'
    self.fldigi_timer = 0
    self.oldRxFreq = 0			# Last value of self.rxFreq
    self.screen = None
    self.audio_volume = 0.0		# Set output volume, 0.0 to 1.0
    self.sidetone_volume = 0
    self.sound_error = 0
    self.sound_thread = None
    self.mode = conf.default_mode
    self.color_list = None
    self.color_index = 0
    self.vardecim_set = None
    self.w_phase = None
    self.zoom = 1.0
    self.filter_bandwidth = 1000
    self.zoom_deltaf = 0
    self.zooming = False
    self.split_rxtx = False	# Are we in split Rx/Tx mode?
    self.savedState = {}
    self.pttButton = None
    self.tmp_playing = False
    # get the screen size - thanks to Lucian Langa
    x, y, self.screen_width, self.screen_height = wx.Display().GetGeometry()
    self.Bind(wx.EVT_IDLE, self.OnIdle)
    self.Bind(wx.EVT_QUERY_END_SESSION, self.OnEndSession)
    # Restore persistent program state
    if conf.persistent_state:
      self.init_path = os.path.join(os.path.dirname(ConfigPath), '.quisk_init.pkl')
      try:
        fp = open(self.init_path, "rb")
        d = pickle.load(fp)
        fp.close()
        for k, v in d.items():
          if k in self.StateNames:
            self.savedState[k] = v
            if k == 'bandState':
              self.bandState.update(v)
            elif k == 'wfallScaleZ':
              self.wfallScaleZ.update(v)
            else:
              setattr(self, k, v)
      except:
        pass #traceback.print_exc()
      for k, (vfo, tune, mode) in self.bandState.items():	# Historical: fix bad frequencies
        try:
          f1, f2 = conf.BandEdge[k]
          if not f1 <= vfo + tune <= f2:
            self.bandState[k] = conf.bandState[k]
        except KeyError:
          pass
    if self.bandAmplPhase and type(self.bandAmplPhase.values()[0]) is not DictType:
      print("""Old sound card amplitude and phase corrections must be re-entered (sorry).
The new code supports multiple corrections per band.""")
      self.bandAmplPhase = {}
    if Hardware.VarDecimGetChoices():	# Hardware can change the decimation.
      self.sample_rate = Hardware.VarDecimSet()	# Get the sample rate.
      self.vardecim_set = self.sample_rate
      try:
        var_rate1, var_rate2 = Hardware.VarDecimRange()
      except:
        var_rate1, var_rate2 = (48000, 960000)
    else:		# Use the sample rate from the config file.
      var_rate1 = None
      self.sample_rate = conf.sample_rate
    if not hasattr(conf, 'playback_rate'):
      if conf.use_sdriq or conf.use_rx_udp:
        conf.playback_rate = 48000
      else:
        conf.playback_rate = conf.sample_rate
    # Check for PulseAudio names and substitute the actual device name for abbreviations
    if sys.platform != 'win32' and conf.show_pulse_audio_devices:
      pa_dev_capt, pa_dev_play = QS.pa_sound_devices()
      for key in ("name_of_sound_play", "name_of_mic_play", "digital_output_name", "sample_playback_name"):
        value = getattr(conf, key)		# playback devices
        if value[0:6] == "pulse:":
          for n0, n1, n2 in pa_dev_play:
            for n in (n0, n1, n2):
              if value[6:] in n:
                setattr(conf, key, "pulse:" + n0)
      for key in ("name_of_sound_capt", "microphone_name", "digital_input_name"):
        value = getattr(conf, key)		# capture devices
        if value[0:6] == "pulse:":
          for n0, n1, n2 in pa_dev_capt:
            for n in (n0, n1, n2):
              if value[6:] in n:
                setattr(conf, key, "pulse:" + n0)
    # Find the data width from a list of prefered sizes; it is the width of returned graph data.
    # The graph_width is the width of data_width that is displayed.
    width = self.screen_width * conf.graph_width
    percent = conf.display_fraction		# display central fraction of total width
    percent = int(percent * 100.0 + 0.4)
    width = width * 100 // percent
    for x in fftPreferedSizes:
      if x > width:
        self.data_width = x
        break
    else:
      self.data_width = fftPreferedSizes[-1]
    self.graph_width = self.data_width * percent // 100
    if self.graph_width % 2 == 1:		# Both data_width and graph_width are even numbers
      self.graph_width += 1
    # The FFT size times the average_count controls the graph refresh rate
    factor = float(self.sample_rate) / conf.graph_refresh / self.data_width
    ifactor = int(factor + 0.5)		# fft size multiplier * average count
    if conf.fft_size_multiplier >= 999:	# Use large FFT and average count 1
      fft_mult = ifactor
      average_count = 1
    elif conf.fft_size_multiplier > 0:		# Specified fft_size_multiplier
      fft_mult = conf.fft_size_multiplier
      average_count = int(factor / fft_mult + 0.5)
      if average_count < 1:
        average_count = 1
    elif var_rate1 is None:		# Calculate an equal split between fft size and average
      fft_mult = 1
      for mult in (32, 27, 24, 18, 16, 12, 9, 8, 6, 4, 3, 2, 1):	# product of small factors
        average_count = int(factor / mult + 0.5)
        if average_count >= mult:
          fft_mult = mult
          break
      average_count = int(factor / fft_mult + 0.5)
      if average_count < 1:
        average_count = 1
    else:		# Calculate a compromise for variable rates
      fft_mult = int(float(var_rate1) / conf.graph_refresh / self.data_width + 0.5)	# large fft_mult at low rate
      if fft_mult > 8:
        fft_mult = 8
      elif fft_mult == 5:
        fft_mult = 4
      elif fft_mult == 7:
        fft_mult = 6
      average_count = int(factor / fft_mult + 0.5)
      if average_count < 1:
        average_count = 1
    self.fft_size = self.data_width * fft_mult
    # print 'data, graph,fft', self.data_width, self.graph_width, self.fft_size
    self.width = self.screen_width * 8 // 10
    self.height = self.screen_height * 5 // 10
    self.main_frame = frame = QMainFrame(self.width, self.height)
    self.SetTopWindow(frame)
    # Record the basic application parameters
    if sys.platform == 'win32':
      h = self.main_frame.GetHandle()
    else:
      h = 0
    QS.record_app(self, conf, self.data_width, self.fft_size,
                 average_count, self.sample_rate, h)
    #print ('FFT size %d, FFT mult %d, average_count %d, rate %d, Refresh %.2f Hz' % (
    #    self.fft_size, self.fft_size / self.data_width, average_count, self.sample_rate,
    #    float(self.sample_rate) / self.fft_size / average_count))
    QS.record_graph(0, 0, 1.0)
    QS.set_tx_audio(vox_level=20, vox_time=self.timeVOX)	# Turn off VOX, set VOX time
    # Make all the screens and hide all but one
    self.graph = GraphScreen(frame, self.data_width, self.graph_width)
    self.screen = self.graph
    width = self.graph.width
    button_width = width	# calculate the final button width
    self.config_screen = ConfigScreen(frame, width, self.fft_size)
    self.config_screen.Hide()
    self.waterfall = WaterfallScreen(frame, width, self.data_width, self.graph_width)
    self.waterfall.Hide()
    self.scope = ScopeScreen(frame, width, self.data_width, self.graph_width)
    self.scope.Hide()
    self.filter_screen = FilterScreen(frame, self.data_width, self.graph_width)
    self.filter_screen.Hide()
    self.help_screen = HelpScreen(frame, width, self.screen_height // 10)
    self.help_screen.Hide()
    self.station_screen = StationScreen(frame, width, conf.station_display_lines)
    self.station_screen.Hide()
    # Make a vertical box to hold all the screens and the bottom box
    vertBox = self.vertBox = wx.BoxSizer(wx.VERTICAL)
    frame.SetSizer(vertBox)
    # Add the screens
    vertBox.Add(self.config_screen, 1, wx.EXPAND)
    vertBox.Add(self.graph, 1)
    vertBox.Add(self.waterfall, 1)
    vertBox.Add(self.scope, 1)
    vertBox.Add(self.filter_screen, 1)
    vertBox.Add(self.help_screen, 1)
    vertBox.Add(self.station_screen)
    # Add the spacer
    vertBox.Add(Spacer(frame), 0, wx.EXPAND)
    # Add the bottom box
    hBoxA = wx.BoxSizer(wx.HORIZONTAL)
    vertBox.Add(hBoxA, 0, wx.EXPAND)
    vertBox.AddSpacer(5)		# Thanks to Christof, DJ4CM
    # End of vertical box.  Add items to the horizontal box.
    # Add two sliders on the left
    margin = 3
    self.sliderVol = SliderBoxV(frame, 'Vol', self.volumeAudio, 1000, self.ChangeVolume)
    button_width -= self.sliderVol.width + margin * 2
    self.ChangeVolume()		# set initial volume level
    hBoxA.Add(self.sliderVol, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, margin)
    if Hardware.use_sidetone:
      self.sliderSto = SliderBoxV(frame, 'STo', 300, 1000, self.ChangeSidetone)
      button_width -= self.sliderSto.width + margin * 2
      self.ChangeSidetone()
      hBoxA.Add(self.sliderSto, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, margin)
    # Add the sizer for the middle
    gap = 2
    gbs = wx.GridBagSizer(gap, gap)
    self.gbs = gbs
    button_width -= gap * 15
    hBoxA.Add(gbs, 1, wx.EXPAND, 0)
    gbs.SetEmptyCellSize((5, 5))
    button_width -= 5
    # Add three sliders on the right
    self.sliderYs = SliderBoxV(frame, 'Ys', 0, 160, self.ChangeYscale, True)
    button_width -= self.sliderYs.width + margin * 2
    hBoxA.Add(self.sliderYs, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, margin)
    self.sliderYz = SliderBoxV(frame, 'Yz', 0, 160, self.ChangeYzero, True)
    button_width -= self.sliderYz.width + margin * 2
    hBoxA.Add(self.sliderYz, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, margin)
    self.sliderZo = SliderBoxV(frame, 'Zo', 0, 1000, self.OnChangeZoom)
    button_width -= self.sliderZo.width + margin * 2
    hBoxA.Add(self.sliderZo, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, margin)
    self.sliderZo.SetValue(0)
    button_width //= 13		# This is our final button size
    bw = button_width
    button_width, button_height = self.MakeButtons(frame, gbs, button_width, gap)
    ww = self.graph.width
    self.main_frame.SetSizeHints(ww, 100)
    if button_width > bw:		# The button width was increased
      ww += (button_width - bw) * 12
    self.main_frame.SetClientSizeWH(ww, self.screen_height * 5 // 10)
    self.MakeTopRow(frame, gbs, button_width, button_height)
    self.button_width = button_width
    self.button_height = button_height
    if conf.quisk_widgets:
      self.bottom_widgets = conf.quisk_widgets.BottomWidgets(self, Hardware, conf, frame, gbs, vertBox)
    if QS.open_key(conf.key_method):
      print('open_key failed for name "%s"' % conf.key_method)
    if hasattr(conf, 'mixer_settings'):
      for dev, numid, value in conf.mixer_settings:
        err_msg = QS.mixer_set(dev, numid, value)
        if err_msg:
          print("Mixer", err_msg)
    # Open the hardware.  This must be called before open_sound().
    self.config_text = Hardware.open()
    if not self.config_text:
      self.config_text = "Missing config_text"
    if conf.use_rx_udp:
      self.add_version = True		# Add firmware version to config text
    else:
      self.add_version = False
    QS.capt_channels (conf.channel_i, conf.channel_q)
    QS.play_channels (conf.channel_i, conf.channel_q)
    QS.micplay_channels (conf.mic_play_chan_I, conf.mic_play_chan_Q)
    # Note: Subsequent calls to set channels must not name a higher channel number.
    #       Normally, these calls are only used to reverse the channels.
    QS.open_sound(conf.name_of_sound_capt, conf.name_of_sound_play, 0,
                conf.data_poll_usec, conf.latency_millisecs,
                conf.microphone_name, conf.tx_ip, conf.tx_audio_port,
                conf.mic_sample_rate, conf.mic_channel_I, conf.mic_channel_Q,
				conf.mic_out_volume, conf.name_of_mic_play, conf.mic_playback_rate)
    tune, vfo = Hardware.ReturnFrequency()	# Request initial frequency
    if tune is None or vfo is None:		# Set last-used frequency
      self.bandBtnGroup.SetLabel(self.lastBand, do_cmd=True)
    else:			# Set requested frequency
      self.BandFromFreq(tune)
      self.ChangeDisplayFrequency(tune - vfo, vfo)
    # Record filter rate for the filter screen
    self.filter_screen.sample_rate = QS.get_filter_rate()
    #if info[8]:		# error message
    #  self.sound_error = 1
    #  self.config_screen.err_msg = info[8]
    #  print info[8]
    self.config_screen.InitBitmap()
    if self.sound_error:
      self.screenBtnGroup.SetLabel('Config', do_cmd=True)
      frame.Show()
    else:
      self.screenBtnGroup.SetLabel(conf.default_screen, do_cmd=True)
      frame.Show()
      self.Yield()
      self.sound_thread = SoundThread()
      self.sound_thread.start()
    if conf.dxClHost:
      # create DX Cluster and register listener for change notification
      self.dxCluster = dxcluster.DxCluster()    
      self.dxCluster.setListener(self.OnDxClChange)
      self.dxCluster.start()
    return True
  def OnDxClChange(self):
    self.station_screen.Refresh()
  def OnIdle(self, event):
    if self.screen:
      self.screen.OnIdle(event)
  def OnEndSession(self, event):
    event.Skip()
    self.OnBtnClose(event)
  def OnBtnClose(self, event):
    QS.set_file_record(3, '')	# Turn off file recording
    time.sleep(0.1)
    if self.sound_thread:
      self.sound_thread.stop()
    for i in range(0, 20):
      if threading.activeCount() == 1:
        break
      time.sleep(0.1)
  def OnExit(self):
    if self.dxCluster:
      self.dxCluster.stop()
    QS.close_rx_udp()
    Hardware.close()
    self.SaveState()
  def CheckState(self):		# check whether state has changed
    changed = False
    if self.init_path:		# save current program state
      for n in self.StateNames:
        try:
          if getattr(self, n) != self.savedState[n]:
            changed = True
            break
        except:
          changed = True
          break
    return changed
  def SaveState(self):
    if self.init_path:		# save current program state
      d = {}
      for n in self.StateNames:
        d[n] = v = getattr(self, n)
        self.savedState[n] = v
      try:
        fp = open(self.init_path, "wb")
        pickle.dump(d, fp)
        fp.close()
      except:
        pass #traceback.print_exc()
  def MakeTopRow(self, frame, gbs, button_width, button_height):
    # Down button
    b_down = QuiskRepeatbutton(frame, self.OnBtnDownBand, conf.Xbtn_text_range_dn,
             self.OnBtnUpDnBandDone, use_right=True)
    gbs.Add(b_down, (0, 8), flag=wx.ALIGN_CENTER)
    # Up button
    b_up = QuiskRepeatbutton(frame, self.OnBtnUpBand, conf.Xbtn_text_range_up,
             self.OnBtnUpDnBandDone, use_right=True)
    gbs.Add(b_up, (0, 9), flag=wx.ALIGN_CENTER)
    # Memory buttons
    b = QuiskPushbutton(frame, self.OnBtnMemSave, conf.Xbtn_text_mem_add)
    gbs.Add(b, (0, 11), flag=wx.ALIGN_CENTER)
    b = self.memNextButton = QuiskPushbutton(frame, self.OnBtnMemNext, conf.Xbtn_text_mem_next)
    b.Enable(False)
    self.Bind(wx.EVT_RIGHT_DOWN, self.OnRightClickMemory, b)
    gbs.Add(b, (0, 12), flag=wx.ALIGN_CENTER)
    b = self.memDeleteButton = QuiskPushbutton(frame, self.OnBtnMemDelete, conf.Xbtn_text_mem_del)
    b.Enable(False)
    gbs.Add(b, (0, 13), flag=wx.ALIGN_CENTER)
    # Favorites buttons
    b = self.StationNewButton = QuiskPushbutton(frame, self.OnBtnFavoritesNew, conf.Xbtn_text_fav_add)
    gbs.Add(b, (0, 15), flag=wx.ALIGN_CENTER)
    b = self.StationNewButton = QuiskPushbutton(frame, self.OnBtnFavoritesShow, conf.Xbtn_text_fav_recall)
    gbs.Add(b, (0, 16), flag=wx.ALIGN_CENTER)        
    # RIT button
    self.ritButton = QuiskCheckbutton(frame, self.OnBtnRit, "RIT")
    gbs.Add(self.ritButton, (0, 17), flag=wx.ALIGN_CENTER)
    # RIT slider
    self.ritScale = SliderBoxHH(frame, '%d ', self.ritFreq, -2000, 2000, self.OnRitScale, True)
    gbs.Add(self.ritScale, (0, 18), (1, 5), flag=wx.EXPAND)
    # Frequency display
    bw, bh = b_down.GetMinSize()
    self.freqDisplay = FrequencyDisplay(frame, 99999, bh * 15 // 10)
    self.freqDisplay.Display(self.txFreq + self.VFO)
    gbs.Add(self.freqDisplay, (0, 0), (1, 6),
       flag=wx.EXPAND | wx.TOP | wx.BOTTOM, border=self.freqDisplay.border)
    # Frequency entry
    e = wx.TextCtrl(frame, -1, '', size=(10, bh), style=wx.TE_PROCESS_ENTER)
    font = wx.Font(10, wx.FONTFAMILY_SWISS, wx.NORMAL, wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface)
    e.SetFont(font)
    e.SetBackgroundColour(conf.color_entry)
    e.SetForegroundColour(conf.color_entry_txt)
    szr = wx.BoxSizer(wx.HORIZONTAL)	# add control to box sizer for centering
    szr.Add(e, 1, flag=wx.ALIGN_LEFT|wx.ALIGN_CENTER_VERTICAL)
    gbs.Add(szr, (0, 6), (1, 2), flag = wx.EXPAND|wx.LEFT|wx.RIGHT, border=5)
    frame.Bind(wx.EVT_TEXT_ENTER, self.FreqEntry, source=e)
    # S-meter
    self.smeter = QuiskText(frame, ' S9+23 -166.00 dB ', bh, wx.ALIGN_LEFT, True)
    gbs.Add(self.smeter, (0, 23), (1, 4), flag=wx.EXPAND)
    self.smeter.TextCtrl.Bind(wx.EVT_RIGHT_DOWN, self.OnSmeterRightDown)
    self.smeter.TextCtrl.SetBackgroundColour(conf.color_freq)
    self.smeter.TextCtrl.SetForegroundColour(conf.color_freq_txt)
    # Make a popup menu for the s-meter
    self.smeter_menu = wx.Menu()
    item = self.smeter_menu.Append(-1, 'S-meter 1')
    self.Bind(wx.EVT_MENU, self.OnSmeterMeterA, item)
    item = self.smeter_menu.Append(-1, 'S-meter 5')
    self.Bind(wx.EVT_MENU, self.OnSmeterMeterB, item)
    item = self.smeter_menu.Append(-1, 'Frequency 2')
    self.Bind(wx.EVT_MENU, self.OnSmeterFrequencyA, item)
    item = self.smeter_menu.Append(-1, 'Frequency 10')
    self.Bind(wx.EVT_MENU, self.OnSmeterFrequencyB, item)
    item = self.smeter_menu.Append(-1, 'Audio 1')
    self.Bind(wx.EVT_MENU, self.OnSmeterAudioA, item)
    item = self.smeter_menu.Append(-1, 'Audio 5')
    self.Bind(wx.EVT_MENU, self.OnSmeterAudioB, item)
    # Make a popup menu for the memory buttons
    self.memory_menu = wx.Menu()
  def OnSmeterRightDown(self, event):
    pos = event.GetPosition()
    self.smeter.TextCtrl.PopupMenu(self.smeter_menu, pos)
  def OnSmeterMeterA(self, event):
    self.smeter_avg_seconds = 1.0
    self.smeter_usage = "smeter"
    QS.measure_frequency(0)
  def OnSmeterMeterB(self, event):
    self.smeter_avg_seconds = 5.0
    self.smeter_usage = "smeter"
    QS.measure_frequency(0)
  def OnSmeterFrequencyA(self, event):
    self.smeter_usage = "freq"
    QS.measure_frequency(2)
  def OnSmeterFrequencyB(self, event):
    self.smeter_usage = "freq"
    QS.measure_frequency(10)
  def OnSmeterAudioA(self, event):
    self.smeter_usage = "audio"
    QS.measure_audio(1)
  def OnSmeterAudioB(self, event):
    self.smeter_usage = "audio"
    QS.measure_audio(5)
  def MakeButtons(self, frame, gbs, button_width, gap):
    # There are fourteen columns, a small gap column, and then twelve more columns
    ### Left bank of buttons in fourteen columns
    flag = wx.EXPAND
    self.bandBtnGroup = RadioButtonGroup(frame, self.OnBtnBand, conf.bandLabels, None)
    band_buttons = self.bandBtnGroup.buttons
    col = 0
    for b in band_buttons[0:14]:
      gbs.Add(b, (1, col), flag=flag)
      col += 1
    for i in range(col, 14):
      b = QuiskCheckbutton(frame, None, text='')
      gbs.Add(b, (1, i), flag=flag)
    # Receive button row: Mute, AGC
    left_row2 = []
    b = QuiskCheckbutton(frame, self.OnBtnMute, text='Mute')
    left_row2.append(b)
    self.BtnAGC = QuiskSliderButton(frame, self.OnBtnAGC, 'AGC')   # AGC and Squelch
    self.BtnAGC.SetDual(True)
    self.BtnAGC.SetSlider(self.levelSquelch, self.levelOffAGC, self.levelAGC)
    left_row2.append(self.BtnAGC)
    b = QuiskCycleCheckbutton(frame, self.OnBtnNB, ('NB', 'NB 1', 'NB 2', 'NB 3'))
    left_row2.append(b)
    b = QuiskCheckbutton(frame, self.OnBtnAutoNotch, text='Notch')
    left_row2.append(b)
    try:
      labels = Hardware.rf_gain_labels
    except:
      labels = ()
    if labels:
      b = self.BtnRfGain = QuiskCycleCheckbutton(frame, Hardware.OnButtonRfGain, labels)
    else:
      b = QuiskCheckbutton(frame, None, text='RfGain')
      b.Enable(False)
      self.BtnRfGain = None
    left_row2.append(b)
    try:
      labels = Hardware.antenna_labels
    except:
      labels = ()
    if labels:
      b = QuiskCycleCheckbutton(frame, Hardware.OnButtonAntenna, labels)
    else:
      b = QuiskCheckbutton(frame, None, text='Ant 1')
      b.Enable(False)
    left_row2.append(b)
    if 0:	# Display a color chooser
      b = QuiskRepeatbutton(frame, self.OnBtnColor, 'Color', use_right=True)
    else:
      b = self.test1Button = QuiskCheckbutton(frame, self.OnBtnTest1, 'Test 1', color=conf.color_test)
    left_row2.append(b)
    # Transmit button row: Spot
    left_row3=[]
    b = QuiskSpotButton(frame, self.OnBtnSpot, 'Spot', slider_value=self.levelSpot,
         color=conf.color_test, slider_min=10, slider_max=1000)
    if not hasattr(Hardware, 'OnSpot'):
      b.Enable(False)
    left_row3.append(b)
    b = self.splitButton = QuiskCheckbutton(frame, self.OnBtnSplit, "Split")
    if conf.mouse_tune_method:		# Mouse motion changes the VFO frequency
      b.Enable(False)
    left_row3.append(b)
    b = QuiskCheckbutton(frame, self.OnBtnFDX, 'FDX', color=conf.color_test)
    if not conf.add_fdx_button:
      b.Enable(False)
    left_row3.append(b)
    if hasattr(Hardware, 'OnButtonPTT'):
      b = QuiskCheckbutton(frame, Hardware.OnButtonPTT, 'PTT', color='red')
      self.pttButton = b
      left_row3.append(b)
      b = QuiskCheckbutton(frame, self.OnButtonVOX, 'VOX')
      left_row3.append(b)
    else:
      b = QuiskCheckbutton(frame, None, 'PTT')
      b.Enable(False)
      left_row3.append(b)
      b = QuiskCheckbutton(frame, None, 'VOX')
      b.Enable(False)
      left_row3.append(b)
    # Record and Playback buttons
    b = self.btnTmpRecord = QuiskCheckbutton(frame, self.OnBtnTmpRecord, text=conf.Xbtn_text_rec)
    left_row3.append(b)
    b = self.btnTmpPlay = QuiskCheckbutton(frame, self.OnBtnTmpPlay, text=conf.Xbtn_text_play)
    b.Enable(0)
    left_row3.append(b)
    self.btn_file_record = QuiskCheckbutton(frame, self.OnBtnFileRecord, conf.Xbtn_text_file_rec)
    if not conf.file_name_audio and not conf.file_name_samples:
      self.btn_file_record.Enable(0)
    left_row3.append(self.btn_file_record)
    b = QuiskCheckbutton(frame, None, text='')
    left_row3.append(b)
    ### Right bank of buttons
    labels = [('CWL', 'CWU'), ('LSB', 'USB'), 'AM', 'FM', ('DGT-U', 'DGT-L', 'DGT-IQ')]
    if conf.add_imd_button:
      labels.append(('IMD', 'IMD -3dB', 'IMD -6dB'))
    elif conf.add_extern_demod:
      labels.append(conf.add_extern_demod)
    else:
      labels.append('')
    self.modeButns = RadioButtonGroup(frame, self.OnBtnMode, labels, None)
    right_row1 = self.modeButns.GetButtons()[:]
    if conf.add_imd_button:
      right_row1[-1].color = conf.color_test
    labels = ('0', '0', '0', '0', '0', '0')
    self.filterButns = RadioButtonGroup(frame, self.OnBtnFilter, labels, None)
    b = QuiskFilterButton(frame, text=str(self.filterAdjBw1))
    self.filterButns.ReplaceButton(5, b)
    right_row2 = self.filterButns.GetButtons()
    labels = (('Graph', 'GraphP1', 'GraphP2'), 'WFall', ('Scope', 'Scope'), 'Config', 'RX Filter', 'Help')
    self.screenBtnGroup = RadioButtonGroup(frame, self.OnBtnScreen, labels, conf.default_screen)
    right_row3 = self.screenBtnGroup.GetButtons()
    col = 0
    for b in left_row2:
      gbs.Add(b, (2, col), (1, 2), flag=flag)
      col += 2
    col = 0
    for b in left_row3:
      if col in (6, 7, 8, 9):		# single column
        gbs.Add(b, (3, col), flag=flag)
        col += 1
      else:					# double column
        gbs.Add(b, (3, col), (1, 2), flag=flag)
        col += 2
    col = 15
    for i in range(0, 6):
      gbs.Add(right_row1[i], (1, col), (1, 2), flag=flag)
      gbs.Add(right_row2[i], (2, col), (1, 2), flag=flag)
      gbs.Add(right_row3[i], (3, col), (1, 2), flag=flag)
      col += 2
    if 1:
      for i in range(14):
        gbs.AddGrowableCol(i,1)
      for i in range(15, 27):
        gbs.AddGrowableCol(i,1)
    w, height = right_row1[0].GetMinSize()
    return button_width, height
  def MeasureAudioVoltage(self):
    v = QS.measure_audio(-1)
    t = "%11.3f" % v
    t = t[0:1] + ' ' + t[1:4] + ' ' + t[4:] + ' uV'
    self.smeter.SetLabel(t)
  def MeasureFrequency(self):
    vfo = Hardware.ReturnVfoFloat()
    if vfo is None:
      vfo = self.VFO
    t = '%13.2f' % (QS.measure_frequency(-1) + vfo)
    t = '  ' + t[0:4] + ' ' + t[4:7] + ' ' + t[7:] + ' Hz'
    self.smeter.SetLabel(t)
  def NewSmeter(self):
    self.smeter_db_count += 1		# count for average
    x = QS.get_smeter()
    self.smeter_db_sum += x		# sum for average
    if self.timer - self.smeter_db_time0 > self.smeter_avg_seconds:		# average time reached
      self.smeter_db = self.smeter_db_sum / self.smeter_db_count
      self.smeter_db_count = self.smeter_db_sum = 0 
      self.smeter_db_time0 = self.timer
    if self.smeter_sunits < x:		# S-meter moves to peak value
      self.smeter_sunits = x
    else:			# S-meter decays at this time constant
      self.smeter_sunits -= (self.smeter_sunits - x) * (self.timer - self.smeter_sunits_time0)
    self.smeter_sunits_time0 = self.timer
    s = self.smeter_sunits / 6.0	# change to S units; 6db per S unit
    s += Hardware.correct_smeter	# S-meter correction for the gain, band, etc.
    if s < 0:
      s = 0
    if s >= 9.5:
      s = (s - 9.0) * 6
      t = "  S9+%2.0f %7.2f dB" % (s, self.smeter_db)
    else:
      t = "  S%.0f    %7.2f dB" % (s, self.smeter_db)
    self.smeter.SetLabel(t)
  def MakeFilterButtons(self, args):
    # Change the filter selections depending on the mode: CW, SSB, etc.
    # Do not change the adjustable filter buttons.
    buttons = self.filterButns.GetButtons()
    for i in range(0, len(buttons) - 1):
      buttons[i].SetLabel(str(args[i]))
      buttons[i].Refresh()
  def MakeFilterCoef(self, rate, N, bw, center):
    """Make an I/Q filter with rectangular passband."""
    lowpass = bw * 24000 // rate // 2
    if lowpass in Filters:
      filtD = Filters[lowpass]
      #print "Custom filter rate %d bandwidth %d" % (rate, bw)
    else:
      #print "Window filter rate %d bandwidth %d" % (rate, bw)
      if N is None:
        shape = 1.5       # Shape factor at 88 dB
        trans = (bw / 2.0 / rate) * (shape - 1.0)     # 88 dB atten
        N = int(4.0 / trans)
        if N > 1000:
          N = 1000
        N = (N // 2) * 2 + 1
      K = bw * N / rate
      filtD = []
      pi = math.pi
      sin = math.sin
      cos = math.cos
      for k in range(-N//2, N//2 + 1):
        # Make a lowpass filter
        if k == 0:
          z = float(K) / N
        else:
          z = 1.0 / N * sin(pi * k * K / N) / sin(pi * k / N)
        # Apply a windowing function
        if 1:	# Blackman window
          w = 0.42 + 0.5 * cos(2. * pi * k / N) + 0.08 * cos(4. * pi * k / N)
        elif 0:	# Hamming
          w = 0.54 + 0.46 * cos(2. * pi * k / N)
        elif 0:	# Hanning
          w = 0.5 + 0.5 * cos(2. * pi * k / N)
        else:
          w = 1
        z *= w
        filtD.append(z)
    if center:
      # Make a bandpass filter by tuning the low pass filter to new center frequency.
      # Make two quadrature filters.
      filtI = []
      filtQ = []
      tune = -1j * 2.0 * math.pi * center / rate;
      NN = len(filtD)
      D = (NN - 1.0) / 2.0;
      for i in range(NN):
        z = 2.0 * cmath.exp(tune * (i - D)) * filtD[i]
        filtI.append(z.real)
        filtQ.append(z.imag)
      return filtI, filtQ
    return filtD, filtD
  def UpdateFilterDisplay(self):
    # Note: Filter bandwidths are ripple bandwidths with a shape factor of 1.2.
    # Also, SSB filters start at 300 Hz.
    if not conf.filter_display:
      size = 1
      tune = 0
    elif self.mode in ('AM', 'FM', 'CWL', 'CWU', 'DGT-IQ'):
      size = int(self.filter_bandwidth / self.zoom / self.sample_rate * self.data_width + 0.5)
      tune = size // 2
    elif self.mode in ('LSB', 'DGT-L'):
      size = int((self.filter_bandwidth + 300) / self.zoom / self.sample_rate * self.data_width + 0.5)
      tune = size - 1
    else:
      size = int((self.filter_bandwidth + 300) / self.zoom / self.sample_rate * self.data_width + 0.5)
      tune = 0
    if size < 2:
      size = 1
      tune = 0
    self.graph.display.UpdateFilterDisplay(size, tune)
    self.waterfall.pane1.display.UpdateFilterDisplay(size, tune)
  def OnBtnFilter(self, event, bw=None):
    if event is None:	# called by application
      self.filterButns.SetLabel(str(bw))
    else:		# called by button
      btn = event.GetEventObject()
      bw = int(btn.GetLabel())
    mode = self.mode
    if mode in ("CWL", "CWU"):
      bw = min(bw, 2500)
      center = max(conf.cwTone, bw//2)
    elif mode in ('LSB', 'USB'):
      bw = min(bw, 5000)
      center = 300 + bw // 2
    elif mode == 'AM':
      bw = min(bw, 21000)
      center = 0
    elif mode == 'FM':
      bw = min(bw, 21000)
      center = 0
    elif mode in ('DGT-U', 'DGT-L'):
      bw = min(bw, 21000)
      center = 300 + bw / 2
    elif mode == 'DGT-IQ':
      bw = min(bw, 21000)
      center = 0
    else:
      bw = min(bw, 5000)
      center = 300 + bw / 2
    self.filter_bandwidth = bw
    self.UpdateFilterDisplay()
    frate = QS.get_filter_rate()
    filtI, filtQ = self.MakeFilterCoef(frate, None, bw, center)
    QS.set_filters(filtI, filtQ, bw)
    if self.screen is self.filter_screen:
      self.screen.NewFilter()
  def OnBtnScreen(self, event, name=None):
    if event is not None:
      win = event.GetEventObject()
      name = win.GetLabel()
    self.screen.Hide()
    self.station_screen.Hide()
    if name == 'Config':
      self.screen = self.config_screen
    elif name[0:5] == 'Graph':
      self.screen = self.graph
      self.screen.SetTxFreq(self.txFreq, self.rxFreq)
      self.freqDisplay.Display(self.VFO + self.txFreq)
      self.screen.PeakHold(name)
      self.station_screen.Show()
    elif name == 'WFall':
      self.screen = self.waterfall
      self.screen.SetTxFreq(self.txFreq, self.rxFreq)
      self.freqDisplay.Display(self.VFO + self.txFreq)
      sash = self.screen.GetSashPosition()
      self.station_screen.Show()
    elif name == 'Scope':
      if win.direction:				# Another push on the same button
        self.scope.running = 1 - self.scope.running		# Toggle run state
      else:				# Initial push of button
        self.scope.running = 1
      self.screen = self.scope
    elif name == 'RX Filter':
      self.screen = self.filter_screen
      self.freqDisplay.Display(self.screen.txFreq)
      self.screen.NewFilter()
    elif name == 'Help':
      self.screen = self.help_screen
    self.screen.Show()
    self.vertBox.Layout()	# This destroys the initialized sash position!
    self.sliderYs.SetValue(self.screen.y_scale)
    self.sliderYz.SetValue(self.screen.y_zero)
    if name == 'WFall':
      self.screen.SetSashPosition(sash)
  def OnBtnFileRecord(self, event):
    if event.GetEventObject().GetValue():
      QS.set_file_record(2, '')
    else:
      QS.set_file_record(3, '')
  def ChangeYscale(self, event):
    self.screen.ChangeYscale(self.sliderYs.GetValue())
    if self.screen == self.waterfall:
      self.wfallScaleZ[self.lastBand] = (self.waterfall.y_scale, self.waterfall.y_zero)
  def ChangeYzero(self, event):
    self.screen.ChangeYzero(self.sliderYz.GetValue())
    if self.screen == self.waterfall:
      self.wfallScaleZ[self.lastBand] = (self.waterfall.y_scale, self.waterfall.y_zero)
  def OnChangeZoom(self, event):
    x = self.sliderZo.GetValue()
    if x < 50:
      self.zoom = 1.0	# change back to not-zoomed mode
      self.zoom_deltaf = 0
      self.zooming = False
    else:
      a = 1000.0 * self.sample_rate / (self.sample_rate - 2500.0)
      self.zoom = 1.0 - x / a
      if not self.zooming:
        self.zoom_deltaf = self.txFreq		# set deltaf when zoom mode starts
        self.zooming = True
    zoom = self.zoom
    deltaf = self.zoom_deltaf
    self.graph.ChangeZoom(zoom, deltaf)
    self.waterfall.pane1.ChangeZoom(zoom, deltaf)
    self.waterfall.pane2.ChangeZoom(zoom, deltaf)
    self.waterfall.pane2.display.ChangeZoom(zoom, deltaf)
    self.screen.SetTxFreq(self.txFreq, self.rxFreq)
    self.UpdateFilterDisplay()
    self.station_screen.Refresh()
  def OnLevelVOX(self, event):
    self.levelVOX = event.GetEventObject().GetValue()
    if self.useVOX:
      QS.set_tx_audio(vox_level=self.levelVOX)
  def OnTimeVOX(self, event):
    self.timeVOX = event.GetEventObject().GetValue()
    QS.set_tx_audio(vox_time=self.timeVOX)
  def OnButtonVOX(self, event):
    self.useVOX = event.GetEventObject().GetValue()
    if self.useVOX:
      QS.set_tx_audio(vox_level=self.levelVOX)
    else:
      QS.set_tx_audio(vox_level=20)
      if self.pttButton.GetValue():
        self.pttButton.SetValue(0, True)
  def OnTxAudioClip(self, event):
    v = event.GetEventObject().GetValue()
    if self.mode in ('USB', 'LSB'):
      self.txAudioClipUsb = v
    elif self.mode == 'AM':
      self.txAudioClipAm = v
    elif self.mode == 'FM':
      self.txAudioClipFm = v
    else:
      return
    QS.set_tx_audio(mic_clip=v)
  def OnTxAudioPreemph(self, event):
    v = event.GetEventObject().GetValue()
    if self.mode in ('USB', 'LSB'):
      self.txAudioPreemphUsb = v
    elif self.mode == 'AM':
      self.txAudioPreemphAm = v
    elif self.mode == 'FM':
      self.txAudioPreemphFm = v
    else:
      return
    QS.set_tx_audio(mic_preemphasis = v * 0.01)
  def SetTxAudio(self):
    if self.mode in ('USB', 'LSB'):
      clp = self.txAudioClipUsb
      pre = self.txAudioPreemphUsb
    elif self.mode == 'AM':
      clp = self.txAudioClipAm
      pre = self.txAudioPreemphAm
    elif self.mode == 'FM':
      clp = self.txAudioClipFm
      pre = self.txAudioPreemphFm
    else:
      return
    QS.set_tx_audio(mic_clip=clp, mic_preemphasis=pre * 0.01)
    self.CtrlTxAudioClip.SetValue(clp)
    self.CtrlTxAudioPreemph.SetValue(pre)
  def OnBtnMute(self, event):
    btn = event.GetEventObject()
    if btn.GetValue():
      QS.set_volume(0)
    else:
      QS.set_volume(self.audio_volume)
  def OnBtnDecimation(self, event):
    i = event.GetSelection()
    rate = Hardware.VarDecimSet(i)
    self.vardecim_set = rate
    if rate != self.sample_rate:
      self.sample_rate = rate
      self.graph.sample_rate = rate
      self.waterfall.pane1.sample_rate = rate
      self.waterfall.pane2.sample_rate = rate
      self.waterfall.pane2.display.sample_rate = rate
      average_count = float(rate) / conf.graph_refresh / self.fft_size
      average_count = int(average_count + 0.5)
      average_count = max (1, average_count)
      QS.change_rate(rate, average_count)
      #print ('FFT size %d, FFT mult %d, average_count %d, rate %d, Refresh %.2f Hz' % (
      #  self.fft_size, self.fft_size / self.data_width, average_count, rate,
      #  float(rate) / self.fft_size / average_count))
      tune = self.txFreq
      vfo = self.VFO
      self.txFreq = self.VFO = -1		# demand change
      self.ChangeHwFrequency(tune, vfo, 'NewDecim')
      self.UpdateFilterDisplay()
  def ChangeVolume(self, event=None):
    # Caution: event can be None
    value = self.sliderVol.GetValue()
    self.volumeAudio = 1000 - value
    # Simulate log taper pot
    x = (10.0 ** (float(value) * 0.003000434077) - 1) / 1000.0
    self.audio_volume = x	# audio_volume is 0 to 1.000
    QS.set_volume(x)
  def ChangeSidetone(self, event=None):
    # Caution: event can be None
    value = self.sliderSto.GetValue()
    self.sidetone_volume = value
    QS.set_sidetone(value, self.ritFreq, conf.keyupDelay)
  def OnRitScale(self, event=None):	# Called when the RIT slider is moved
    # Caution: event can be None
    if self.ritButton.GetValue():
      value = self.ritScale.GetValue()
      value = int(value)
      self.ritFreq = value
      QS.set_tune(self.rxFreq + self.ritFreq, self.txFreq)
      QS.set_sidetone(self.sidetone_volume, self.ritFreq, conf.keyupDelay)
  def OnBtnSplit(self, event):	# Called when the Split check button is pressed
    self.split_rxtx = self.splitButton.GetValue()
    if self.split_rxtx:
      QS.set_split_rxtx(conf.split_rxtx)
      self.rxFreq = self.oldRxFreq
      d = self.sample_rate * 49 // 100	# Move rxFreq on-screen
      if self.rxFreq < -d:
        self.rxFreq = -d
      elif self.rxFreq > d:
        self.rxFreq = d
    else:
      QS.set_split_rxtx(0)
      self.oldRxFreq = self.rxFreq
      self.rxFreq = self.txFreq
    self.screen.SetTxFreq(self.txFreq, self.rxFreq)
    QS.set_tune(self.rxFreq + self.ritFreq, self.txFreq)
  def OnBtnRit(self, event=None):	# Called when the RIT check button is pressed
    # Caution: event can be None
    if self.ritButton.GetValue():
      self.ritFreq = self.ritScale.GetValue()
    else:
      self.ritFreq = 0
    QS.set_tune(self.rxFreq + self.ritFreq, self.txFreq)
    QS.set_sidetone(self.sidetone_volume, self.ritFreq, conf.keyupDelay)
  def SetRit(self, freq):
    if freq:
      self.ritButton.SetValue(1)
    else:
      self.ritButton.SetValue(0)
    self.ritScale.SetValue(freq)
    self.OnBtnRit()
  def OnBtnFDX(self, event):
    btn = event.GetEventObject()
    if btn.GetValue():
      QS.set_fdx(1)
      if hasattr(Hardware, 'OnBtnFDX'):
        Hardware.OnBtnFDX(1)
    else:
      QS.set_fdx(0)
      if hasattr(Hardware, 'OnBtnFDX'):
        Hardware.OnBtnFDX(0)
  def OnBtnSpot(self, event):
    btn = event.GetEventObject()
    self.levelSpot = btn.slider_value
    if btn.GetValue():
      value = btn.slider_min + btn.slider_max - btn.slider_value	# slider values are backwards in Wx
    else:
      value = 0
    QS.set_spot_level(value)
    Hardware.OnSpot(value)
  def OnBtnTmpRecord(self, event):
    btn = event.GetEventObject()
    if btn.GetValue():
      self.btnTmpPlay.Enable(0)
      QS.set_record_state(0)
    else:
      self.btnTmpPlay.Enable(1)
      QS.set_record_state(1)
  def OnBtnTmpPlay(self, event):
    btn = event.GetEventObject()
    if btn.GetValue():
      if QS.is_key_down() and conf.mic_sample_rate != conf.playback_rate:
        self.btnTmpPlay.SetValue(False, False)
      else:
        self.btnTmpRecord.Enable(0)
        QS.set_record_state(2)
        self.tmp_playing = True
    else:
      self.btnTmpRecord.Enable(1)
      QS.set_record_state(3)
      self.tmp_playing = False
  def OnBtnTest1(self, event):
    btn = event.GetEventObject()
    if btn.GetValue():
      QS.add_tone(10000)
    else:
      QS.add_tone(0)
  def OnBtnTest2(self, event):
    return
  def OnBtnColor(self, event):
    if not self.color_list:
      clist = wx.lib.colourdb.getColourInfoList()
      self.color_list = [(0, clist[0][0])]
      self.color_index = 0
      for i in range(1, len(clist)):
        if  self.color_list[-1][1].replace(' ', '') != clist[i][0].replace(' ', ''):
          #if 'BLUE' in clist[i][0]:
            self.color_list.append((i, clist[i][0]))
    btn = event.GetEventObject()
    if btn.shift:
      del self.color_list[self.color_index]
    else:
      self.color_index += btn.direction
    if self.color_index >= len(self.color_list):
      self.color_index = 0
    elif self.color_index < 0:
      self.color_index = len(self.color_list) -1
    color = self.color_list[self.color_index][1]
    print(self.color_index, color)
    self.main_frame.SetBackgroundColour(color)
    self.main_frame.Refresh()
    self.screen.Refresh()
    btn.SetBackgroundColour(color)
    btn.Refresh()
  def OnBtnAGC(self, event):    # This is a combined AGC and Squelch button
    btn = event.GetEventObject()
    self.levelSquelch = btn.slider_value
    self.levelOffAGC = btn.slider_value_off
    self.levelAGC = btn.slider_value_on
    if self.mode == 'FM':   # This is a Squelch button
      self.use_squelch = btn.GetValue()
      if self.use_squelch:
        value = 1000 - self.levelSquelch		# slider values are backwards in Wx
        QS.set_squelch(value / 12.0 - 120.0)
      else:
        QS.set_squelch(-999.0)
    else:                   # This is an AGC button
      self.use_AGC = btn.GetValue()
      if self.use_AGC:
        level = self.levelAGC
      else:
        level = self.levelOffAGC
      # Simulate log taper pot.  Volume is 0 to 1.
      x = (10.0 ** (float(1000 - level) * 0.003000434077) - 0.99999) / 1000.0
      QS.set_agc(x * conf.agc_max_gain)
  def OnBtnAutoNotch(self, event):
    if event.GetEventObject().GetValue():
      QS.set_auto_notch(1)
    else:
      QS.set_auto_notch(0)
  def OnBtnNB(self, event):
    index = event.GetEventObject().index
    QS.set_noise_blanker(index)
  def FreqEntry(self, event):
    freq = event.GetString()
    if not freq:
      return
    try:
      freq = str2freq (freq)
    except ValueError:
      win = event.GetEventObject()
      win.Clear()
      win.AppendText("Error")
    else:
      tune = freq % 10000
      vfo = freq - tune
      self.BandFromFreq(freq)
      self.ChangeHwFrequency(tune, vfo, 'FreqEntry')
  def ChangeHwFrequency(self, tune, vfo, source='', band='', event=None):
    """Change the VFO and tuning frequencies, and notify the hardware.

    tune:   the new tuning frequency in +- sample_rate/2;
    vfo:    the new vfo frequency in Hertz; this is the RF frequency at zero Hz audio
    source: a string indicating the source or widget requesting the change;
    band:   if source is "BtnBand", the band requested;
    event:  for a widget, the event (used to access control/shift key state).

    Try to update the hardware by calling Hardware.ChangeFrequency().
    The hardware will reply with the updated frequencies which may be different
    from those requested; use and display the returned tune and vfo.
    """
    tune, vfo = Hardware.ChangeFrequency(vfo + tune, vfo, source, band, event)
    self.ChangeDisplayFrequency(tune - vfo, vfo)
  def ChangeDisplayFrequency(self, tune, vfo):
    'Change the frequency displayed by Quisk'
    change = 0
    if tune != self.txFreq:
      change = 1
      self.txFreq = tune
      if not self.split_rxtx:
        self.rxFreq = self.txFreq
      self.screen.SetTxFreq(self.txFreq, self.rxFreq)
      QS.set_tune(self.rxFreq + self.ritFreq, self.txFreq)
    if vfo != self.VFO:
      change = 1
      self.VFO = vfo
      self.graph.SetVFO(vfo)
      self.waterfall.SetVFO(vfo)
      self.station_screen.Refresh()
      if self.w_phase:		# Phase adjustment screen can not change its VFO
        self.w_phase.Destroy()
        self.w_phase = None
      ampl, phase = self.GetAmplPhase(0)
      QS.set_ampl_phase(ampl, phase, 0)
      ampl, phase = self.GetAmplPhase(1)
      QS.set_ampl_phase(ampl, phase, 1)
    if change:
      self.freqDisplay.Display(self.txFreq + self.VFO)
      self.fldigi_new_freq = self.txFreq + self.VFO
    return change
  def ChangeRxTxFrequency(self, rx_freq=None, tx_freq=None):
    if not self.split_rxtx and not tx_freq:
      tx_freq = rx_freq
    if tx_freq:
      tune = tx_freq - self.VFO
      d = self.sample_rate * 45 // 100
      if -d <= tune <= d:	# Frequency is on-screen
        vfo = self.VFO
      else:					# Change the VFO
        vfo = (tx_freq // 5000) * 5000 - 5000
        tune = tx_freq - vfo
        self.BandFromFreq(tx_freq)
      self.ChangeHwFrequency(tune, vfo, 'FreqEntry')
    if rx_freq and self.split_rxtx:		# Frequency must be on-screen
      tune = rx_freq - self.VFO
      self.rxFreq = tune
      self.screen.SetTxFreq(self.txFreq, tune)
      QS.set_tune(tune + self.ritFreq, self.txFreq)
  def OnBtnMode(self, event, mode=None):
    if event is None:	# called by application
      self.modeButns.SetLabel(mode)
    else:		# called by button
      mode = self.modeButns.GetLabel()
    Hardware.ChangeMode(mode)
    self.mode = mode
    if mode == 'CWL':
      QS.set_rx_mode(0)
      self.SetRit(conf.cwTone)
      self.MakeFilterButtons(conf.FilterBwCW)
      self.OnBtnFilter(None, conf.FilterBwCW[3])
    elif mode == 'CWU':
      QS.set_rx_mode(1)
      self.SetRit(-conf.cwTone)
      self.MakeFilterButtons(conf.FilterBwCW)
      self.OnBtnFilter(None, conf.FilterBwCW[3])
    elif mode == 'LSB':
      QS.set_rx_mode(2)
      self.SetRit(0)
      self.MakeFilterButtons(conf.FilterBwSSB)
      self.OnBtnFilter(None, conf.FilterBwSSB[3])
    elif mode == 'USB':
      QS.set_rx_mode(3)
      self.SetRit(0)
      self.MakeFilterButtons(conf.FilterBwSSB)
      self.OnBtnFilter(None, conf.FilterBwSSB[3])
    elif mode == 'AM':
      QS.set_rx_mode(4)
      self.SetRit(0)
      self.MakeFilterButtons(conf.FilterBwAM)
      self.OnBtnFilter(None, conf.FilterBwAM[3])
    elif mode == 'FM':
      QS.set_rx_mode(5)
      self.SetRit(0)
      self.MakeFilterButtons(conf.FilterBwFM)
      self.OnBtnFilter(None, conf.FilterBwFM[3])
    elif mode[0:4] == 'DGT-':
      if mode == 'DGT-U':
        QS.set_rx_mode(7)
      elif mode == 'DGT-L':
        QS.set_rx_mode(8)
      elif mode == 'DGT-IQ':
        QS.set_rx_mode(9)
      else:
        QS.set_rx_mode(7)
      self.SetRit(0)
      self.MakeFilterButtons(conf.FilterBwDGT)
      self.OnBtnFilter(None, conf.FilterBwDGT[1])
    elif mode[0:3] == 'IMD':
      QS.set_rx_mode(10 + self.modeButns.GetSelectedButton().index)	# 10, 11, 12
      self.SetRit(0)
      self.MakeFilterButtons(conf.FilterBwIMD)
      self.OnBtnFilter(None, conf.FilterBwIMD[3])
    elif mode == conf.add_extern_demod:	# External demodulation
      QS.set_rx_mode(6)
      self.SetRit(0)
      self.MakeFilterButtons(conf.FilterBwEXT)
      self.OnBtnFilter(None, conf.FilterBwEXT[3])
    if mode == 'FM':
      self.BtnAGC.SetDual(False)
      self.BtnAGC.SetLabel('Sqlch')
      self.BtnAGC.SetValue(self.use_squelch, True)
    else:
      self.BtnAGC.SetDual(True)
      self.BtnAGC.SetLabel('AGC')
      self.BtnAGC.SetValue(self.use_AGC, True)
    self.SetTxAudio()
  def MakeMemPopMenu(self):
    self.memory_menu.Destroy()
    self.memory_menu = wx.Menu()
    for data in self.memoryState:
      txt = FreqFormatter(data[0])
      item = self.memory_menu.Append(-1, txt)
      self.Bind(wx.EVT_MENU, self.OnPopupMemNext, item)
  def OnPopupMemNext(self, event):
    frq = self.memory_menu.GetLabel(event.GetId())
    frq = frq.replace(' ','')
    frq = int(frq)
    for freq, band, vfo, txfreq, mode in self.memoryState:
      if freq == frq:
        break
    else:
      return
    if band == self.lastBand:	# leave band unchanged
      self.OnBtnMode(None, mode)
      self.ChangeHwFrequency(txfreq, vfo, 'FreqEntry')
    else:		# change to new band
      self.bandState[band] = (vfo, txfreq, mode)
      self.bandBtnGroup.SetLabel(band, do_cmd=True)
  def OnBtnMemSave(self, event):
    frq = self.VFO + self.txFreq
    for i in range(len(self.memoryState)):
      data = self.memoryState[i]
      if data[0] == frq:
        self.memoryState[i] = (self.VFO + self.txFreq, self.lastBand, self.VFO, self.txFreq, self.mode)
        return
    self.memoryState.append((self.VFO + self.txFreq, self.lastBand, self.VFO, self.txFreq, self.mode))
    self.memoryState.sort()
    self.memNextButton.Enable(True)
    self.memDeleteButton.Enable(True)
    self.MakeMemPopMenu()
    self.station_screen.Refresh()
  def OnBtnMemNext(self, event):
    frq = self.VFO + self.txFreq
    for freq, band, vfo, txfreq, mode in self.memoryState:
      if freq > frq:
        break
    else:
      freq, band, vfo, txfreq, mode = self.memoryState[0]
    if band == self.lastBand:	# leave band unchanged
      self.OnBtnMode(None, mode)
      self.ChangeHwFrequency(txfreq, vfo, 'FreqEntry')
    else:		# change to new band
      self.bandState[band] = (vfo, txfreq, mode)
      self.bandBtnGroup.SetLabel(band, do_cmd=True)
  def OnBtnMemDelete(self, event):
    frq = self.VFO + self.txFreq
    for i in range(len(self.memoryState)):
      data = self.memoryState[i]
      if data[0] == frq:
        del self.memoryState[i]
        break
    self.memNextButton.Enable(bool(self.memoryState))
    self.memDeleteButton.Enable(bool(self.memoryState))
    self.MakeMemPopMenu()
    self.station_screen.Refresh()
  def OnRightClickMemory(self, event):
    event.Skip()
    pos = event.GetPosition()
    self.memNextButton.PopupMenu(self.memory_menu, pos)
  def OnBtnFavoritesShow(self, event):
    self.screenBtnGroup.SetLabel("Config", do_cmd=False)
    self.screen.Hide()
    self.screen = self.config_screen
    self.config_screen.notebook.SetSelection(3)
    self.screen.Show()
    self.vertBox.Layout()    # This destroys the initialized sash position!
  def OnBtnFavoritesNew(self, event):
    self.config_screen.favorites.AddNewFavorite();
    self.OnBtnFavoritesShow(event)
  def OnBtnBand(self, event):
    band = self.lastBand	# former band in use
    try:
      f1, f2 = conf.BandEdge[band]
      if f1 <= self.VFO + self.txFreq <= f2:
        self.bandState[band] = (self.VFO, self.txFreq, self.mode)
    except KeyError:
      pass
    btn = event.GetEventObject()
    band = btn.GetLabel()	# new band
    self.lastBand = band
    try:
      vfo, tune, mode = self.bandState[band]
    except KeyError:
      vfo, tune, mode = (0, 0, 'LSB')
    if band == '60':
      if self.mode in ('CWL', 'CWU'):
        freq60 = []
        for f in conf.freq60:
          freq60.append(f + 1500)
      else:
        freq60 = conf.freq60
      freq = vfo + tune
      if btn.direction:
        vfo = self.VFO
        if 5100000 < vfo < 5600000:
          if btn.direction > 0:		# Move up
            for f in freq60:
              if f > vfo + self.txFreq:
                freq = f
                break
            else:
              freq = freq60[0]
          else:			# move down
            l = list(freq60)
            l.reverse()
            for f in l: 
              if f < vfo + self.txFreq:
                freq = f
                break
              else:
                freq = freq60[-1]
      half = self.sample_rate // 2 * self.graph_width // self.data_width
      while freq - vfo <= -half + 1000:
        vfo -= 10000
      while freq - vfo >= +half - 5000:
        vfo += 10000
      tune = freq - vfo
    elif band == 'Time':
      vfo, tune, mode = conf.bandTime[btn.index]
    self.OnBtnMode(None, mode)
    self.txFreq = self.VFO = -1		# demand change
    self.ChangeBand(band)
    self.ChangeHwFrequency(tune, vfo, 'BtnBand', band=band)
  def BandFromFreq(self, frequency):	# Change to a new band based on the frequency
    try:
      f1, f2 = conf.BandEdge[self.lastBand]
      if f1 <= frequency <= f2:
        return						# We are within the current band
    except KeyError:
      f1 = f2 = -1
    # Frequency is not within the current band.  Save the current band data.
    if f1 <= self.VFO + self.txFreq <= f2:
      self.bandState[self.lastBand] = (self.VFO, self.txFreq, self.mode)
    # Change to the correct band based on frequency.
    for band, (f1, f2) in conf.BandEdge.items():
      if f1 <= frequency <= f2:
        self.lastBand = band
        self.bandBtnGroup.SetLabel(band, do_cmd=False)
        try:
          vfo, tune, mode = self.bandState[band]
        except KeyError:
          vfo, tune, mode = (0, 0, 'LSB')
        self.OnBtnMode(None, mode)
        self.ChangeBand(band)
        break
  def ChangeBand(self, band):
    Hardware.ChangeBand(band)
    self.waterfall.SetPane2(self.wfallScaleZ.get(band, (conf.waterfall_y_scale, conf.waterfall_y_zero)))
    if self.screen == self.waterfall:
      self.sliderYs.SetValue(self.screen.y_scale)
      self.sliderYz.SetValue(self.screen.y_zero)
  def OnBtnUpDnBandDelta(self, event, is_band_down):
    sample_rate = int(self.sample_rate * self.zoom)
    oldvfo = self.VFO
    btn = event.GetEventObject()
    if btn.direction > 0:		# left button was used, move a bit
      d = int(sample_rate // 9)
    else:						# right button was used, move to edge
      d = int(sample_rate * 45 // 100)
    if is_band_down:
      d = -d
    vfo = self.VFO + d
    if sample_rate > 40000:
      vfo = (vfo + 5000) // 10000 * 10000	# round to even number
      delta = 10000
    elif sample_rate > 5000:
      vfo = (vfo + 500) // 1000 * 1000
      delta = 1000
    else:
      vfo = (vfo + 50) // 100 * 100
      delta = 100
    if oldvfo == vfo:
      if is_band_down:
        d = -delta
      else:
        d = delta
    else:
      d = vfo - oldvfo
    self.VFO += d
    self.txFreq -= d
    self.rxFreq -= d
    # Set the display but do not change the hardware
    self.graph.SetVFO(self.VFO)
    self.waterfall.SetVFO(self.VFO)
    self.station_screen.Refresh()
    self.screen.SetTxFreq(self.txFreq, self.rxFreq)
    self.freqDisplay.Display(self.txFreq + self.VFO)
  def OnBtnDownBand(self, event):
    self.band_up_down = 1
    self.OnBtnUpDnBandDelta(event, True)
  def OnBtnUpBand(self, event):
    self.band_up_down = 1
    self.OnBtnUpDnBandDelta(event, False)
  def OnBtnUpDnBandDone(self, event):
    self.band_up_down = 0
    tune = self.txFreq
    vfo = self.VFO
    self.txFreq = self.VFO = 0		# Force an update
    self.ChangeHwFrequency(tune, vfo, 'BtnUpDown')
  def GetAmplPhase(self, is_tx):
    if "panadapter" in conf.bandAmplPhase:
      band = "panadapter"
    else:
      band = self.lastBand
    try:
      if is_tx:
        lst = self.bandAmplPhase[band]["tx"]
      else:
        lst = self.bandAmplPhase[band]["rx"]
    except KeyError:
      return (0.0, 0.0)
    length = len(lst)
    if length == 0:
      return (0.0, 0.0)
    elif length == 1:
      return lst[0][2], lst[0][3]
    elif self.VFO < lst[0][0]:		# before first data point
      i1 = 0
      i2 = 1
    elif lst[length - 1][0] < self.VFO:	# after last data point
      i1 = length - 2
      i2 = length - 1
    else:
      # Binary search for the bracket VFO
      i1 = 0
      i2 = length
      index = (i1 + i2) // 2
      for i in range(length):
        diff = lst[index][0] - self.VFO
        if diff < 0:
          i1 = index
        elif diff > 0:
          i2 = index
        else:		# equal VFO's
          return lst[index][2], lst[index][3]
        if i2 - i1 <= 1:
          break
        index = (i1 + i2) // 2
    d1 = self.VFO - lst[i1][0]		# linear interpolation
    d2 = lst[i2][0] - self.VFO
    dx = d1 + d2
    ampl = (d1 * lst[i2][2] + d2 * lst[i1][2]) / dx
    phas = (d1 * lst[i2][3] + d2 * lst[i1][3]) / dx
    return ampl, phas
  def PostStartup(self):	# called once after sound attempts to start
    self.config_screen.OnGraphData(None)	# update config in case sound is not running
  def FldigiPoll(self):		# Keep Quisk and Fldigi frequencies equal; control Fldigi PTT from Quisk
    if self.fldigi_server is None:
      return
    if self.fldigi_new_freq:	# Our frequency changed; send to fldigi
      try:
        self.fldigi_server.main.set_frequency(float(self.fldigi_new_freq))
      except:
        # traceback.print_exc()
        pass
      self.fldigi_new_freq = None
      self.fldigi_timer = time.time()
      return
    try:
      freq = self.fldigi_server.main.get_frequency()
    except:
      # traceback.print_exc()
      return
    else:
      freq = int(freq + 0.5)
    try:
      rxtx = self.fldigi_server.main.get_trx_status()	# returns rx, tx, tune
    except:
      return
    if time.time() - self.fldigi_timer < 0.3:		# If timer is small, change originated in Quisk
      self.fldigi_rxtx = rxtx
      self.fldigi_freq = freq
      return
    if self.fldigi_freq != freq:
      self.fldigi_freq = freq
      #print "Change freq", freq
      self.ChangeRxTxFrequency(None, freq)
      self.fldigi_new_freq = None
    if self.fldigi_rxtx != rxtx:
      self.fldigi_rxtx = rxtx
      #print 'Fldigi changed to', rxtx
      if self.pttButton:
        if rxtx == 'rx':
          self.pttButton.SetValue(0, True)
        else:
          self.pttButton.SetValue(1, True)
        self.fldigi_timer = time.time()
    else:
      if QS.is_key_down():
        if rxtx == 'rx':
          self.fldigi_server.main.tx()
          self.fldigi_timer = time.time()
      else:	# key is up
        if rxtx != 'rx':
          self.fldigi_server.main.rx()
          self.fldigi_timer = time.time()
  def HamlibPoll(self):		# Poll for Hamlib commands
    if self.hamlib_socket:
      try:		# Poll for new client connections.
        conn, address = self.hamlib_socket.accept()
      except socket.error:
        pass
      else:
        # print 'Connection from', address
        self.hamlib_clients.append(HamlibHandler(self, conn, address))
      for client in self.hamlib_clients:	# Service existing clients
        if not client.Process():		# False return indicates a closed connection; remove the handler for this client
          self.hamlib_clients.remove(client)
          # print 'Remove', client.address
          break
  def OnReadSound(self):	# called at frequent intervals
    if self.pttButton:	# Manage the PTT button using VOX and hot keys
      ptt = None
      if self.useVOX:
        if QS.is_vox():
          ptt = True
        else:
          ptt = False
      if conf.hot_key_ptt1:
        hot_key = wx.GetKeyState(conf.hot_key_ptt1) and (conf.hot_key_ptt2 is None or wx.GetKeyState(conf.hot_key_ptt2))
        if hot_key:
          self.hot_key_ptt_on = True
          ptt = True
        elif self.hot_key_ptt_on:
          self.hot_key_ptt_on = False
          if not self.useVOX:
            ptt = False
      if ptt is True and not self.pttButton.GetValue():
        self.pttButton.SetValue(1, True)
      elif ptt is False and self.pttButton.GetValue():
        self.pttButton.SetValue(0, True)
    self.timer = time.time()
    if self.screen == self.scope:
      data = QS.get_graph(0, 1.0, 0)	# get raw data
      if data:
        self.scope.OnGraphData(data)			# Send message to draw new data
        return 1		# we got new graph/scope data
    elif False and self.screen == self.filter_screen:
      data = QS.get_audio_graph()	# Display the audio FFT in the filter window
      if data:
        self.screen.data = data
        self.screen.OnGraphData(data)
    else:
      data = QS.get_graph(1, self.zoom, float(self.zoom_deltaf))	# get FFT data
      if data:
        #T('')
        if self.smeter_usage == "smeter":
          self.NewSmeter()			# update the S-meter
        elif self.smeter_usage == "freq":
          self.MeasureFrequency()	# display measured frequency
        else:
          self.MeasureAudioVoltage()		# display audio voltage
        if self.screen == self.graph:
          self.waterfall.OnGraphData(data)		# save waterfall data
          self.graph.OnGraphData(data)			# Send message to draw new data
        elif self.screen == self.config_screen:
          pass
        else:
          self.screen.OnGraphData(data)			# Send message to draw new data
        #T('graph data')
        #application.Yield()
        #T('Yield')
        return 1		# We got new graph/scope data
    if QS.get_overrange():
      self.clip_time0 = self.timer
      self.freqDisplay.Clip(1)
    if self.clip_time0:
      if self.timer - self.clip_time0 > 1.0:
        self.clip_time0 = 0
        self.freqDisplay.Clip(0)
    if self.timer - self.heart_time0 > 0.10:		# call hardware to perform background tasks
      self.heart_time0 = self.timer
      if self.screen == self.config_screen:
        self.screen.OnGraphData()			# Send message to draw new data
      Hardware.HeartBeat()
      if self.add_version and Hardware.GetFirmwareVersion() is not None:
        self.add_version = False
        self.config_text = "%s, firmware version 1.%d" % (self.config_text, Hardware.GetFirmwareVersion())
      if not self.band_up_down:
        # Poll the hardware for changed frequency.  This is used for hardware
        # that can change its frequency independently of Quisk; eg. K3.
        tune, vfo = Hardware.ReturnFrequency()
        if tune is not None and vfo is not None:
          self.BandFromFreq(tune)
          self.ChangeDisplayFrequency(tune - vfo, vfo)
        self.FldigiPoll()
        self.HamlibPoll()
      if self.timer - self.save_time0 > 20.0:
        self.save_time0 = self.timer
        if self.CheckState():
          self.SaveState()
      if self.tmp_playing and QS.set_record_state(-1):	# poll to see if playback is finished
        self.btnTmpPlay.SetValue(False, True)

def main():
  """If quisk is installed as a package, you can run it with quisk.main()."""
  App()
  application.MainLoop()

if __name__ == '__main__':
  main()

