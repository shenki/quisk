#! /usr/bin/python

# All QUISK software is Copyright (C) 2006-2011 by James C. Ahlstrom.
# This free software is licensed for use under the GNU General Public
# License (GPL), see http://www.opensource.org.
# Note that there is NO WARRANTY AT ALL.  USE AT YOUR OWN RISK!!

"""The main program for Quisk VNA, a vector network analyzer.

Usage:  python quisk_vns.py [-c | --config config_file_path]
This can also be installed as a package and run as quisk_vna.main().
"""

from __future__ import print_function

import sys, os
os.chdir(os.path.normpath(os.path.dirname(__file__)))
if sys.path[0] != "'.'":		# Make sure the current working directory is on path
  sys.path.insert(0, '.')

import wx, wx.html, wx.lib.buttons, wx.lib.stattext, wx.lib.colourdb
import math, cmath, time, traceback
import threading, webbrowser
import _quisk as QS
from types import *
from quisk_widgets import *

DEBUG = 0

# Command line parsing: be able to specify the config file.
from optparse import OptionParser
parser = OptionParser()
parser.add_option('-c', '--config', dest='config_file_path',
		help='Specify the configuration file path')
argv_options = parser.parse_args()[0]
ConfigPath = argv_options.config_file_path	# Get config file path
if not ConfigPath:	# Use default path
  if sys.platform == 'win32':
    path = os.getenv('HOMEDRIVE', '') + os.getenv('HOMEPATH', '')
    for dir in ("Documents", "My Documents", "Eigene Dateien", "Documenti"):
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

class SoundThread(threading.Thread):
  """Create a second (non-GUI) thread to read samples."""
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

class GraphDisplay(wx.Window):
  """Display the graph within the graph screen."""
  def __init__(self, parent, x, y, graph_width, height, chary):
    wx.Window.__init__(self, parent,
       pos = (x, y),
       size = (graph_width, height),
       style = wx.NO_BORDER)
    self.parent = parent
    self.chary = chary
    self.graph_width = graph_width
    self.line_mag = []
    self.line_phase = []
    self.line_swr = []
    self.SetBackgroundColour(conf.color_graph)
    self.Bind(wx.EVT_PAINT, self.OnPaint)
    self.Bind(wx.EVT_LEFT_DOWN, parent.OnLeftDown)
    self.Bind(wx.EVT_LEFT_UP, parent.OnLeftUp)
    self.Bind(wx.EVT_MOTION, parent.OnMotion)
    self.Bind(wx.EVT_MOUSEWHEEL, parent.OnWheel)
    self.tune_tx = graph_width / 2	# Current X position of the Tx tuning line
    self.height = 10
    self.y_min = 1000
    self.y_max = 0
    self.y_ticks = []
    self.max_height = application.screen_height
    self.tuningPenTx = wx.Pen('Red', 1)
    self.magnPen = wx.Pen('Black', 1)
    self.phasePen = wx.Pen((0, 180, 0), 1)
    self.swrPen = wx.Pen('Blue', 1)
    self.backgroundPen = wx.Pen(self.GetBackgroundColour(), 1)
    self.horizPen = wx.Pen(conf.color_gl, 1, wx.SOLID)
    if sys.platform == 'win32':
      self.Bind(wx.EVT_ENTER_WINDOW, self.OnEnter)
  def OnEnter(self, event):
    self.SetFocus()	# Set focus so we get mouse wheel events
  def OnPaint(self, event):
    dc = wx.PaintDC(self)
    x = self.tune_tx
    dc.SetPen(self.tuningPenTx)
    dc.DrawLine(x, 0, x, self.max_height)
    dc.SetPen(self.horizPen)
    for y in self.y_ticks:
      dc.DrawLine(0, y, self.graph_width, y)
    # Magnitude
    t = 'Magnitude, '
    x = self.chary
    y = self.height - self.chary
    dc.SetTextForeground(self.magnPen.GetColour())
    dc.DrawText(t, x, y)
    w, h = dc.GetTextExtent(t)
    x += w + self.chary
    # Phase
    t = 'Phase, '
    dc.SetTextForeground(self.phasePen.GetColour())
    dc.DrawText(t, x, y)
    w, h = dc.GetTextExtent(t)
    x += w + self.chary
    # SWR
    t = 'SWR'
    dc.SetTextForeground(self.swrPen.GetColour())
    dc.DrawText(t, x, y)
    w, h = dc.GetTextExtent(t)
    x += w + self.chary
    # Draw graph
    if self.line_phase:		# Phase line
      # Try to avoid drawing vertical lines when the phase goes from +180 to -180
      dc.SetPen(self.phasePen)
      top = self.y_ticks[0]
      high = self.y_ticks[1]
      low = self.y_ticks[-2]
      bottom = self.y_ticks[-1]
      old_phase = self.line_phase[0]
      line = [(0, old_phase)]
      for x in range(1, self.graph_width):
        phase = self.line_phase[x]
        if phase < high and old_phase > low:
          line.append((x-1, bottom))
          dc.DrawLines(line)
          line = [(x, top), (x, phase)]
        elif phase > low and old_phase < high:
          line.append((x-1, top))
          dc.DrawLines(line)
          line = [(x, bottom), (x, phase)]
        else:
          line.append((x, phase))
        old_phase = phase
      dc.DrawLines(line)
    if self.line_mag:		# Magnitude line
      dc.SetPen(self.magnPen)
      dc.DrawLines(self.line_mag)
    if self.line_swr:		# SWR line
      dc.SetPen(self.swrPen)
      dc.DrawLines(self.line_swr)
  def SetHeight(self, height):
    self.height = height
    self.SetSize((self.graph_width, height))
  def SetTuningLine(self, tune_tx):
    dc = wx.ClientDC(self)
    dc.SetPen(self.backgroundPen)
    dc.DrawLine(self.tune_tx, 0, self.tune_tx, self.max_height)
    dc.SetPen(self.tuningPenTx)
    dc.DrawLine(tune_tx, 0, tune_tx, self.max_height)
    self.tune_tx = tune_tx
    self.Refresh()

class GraphScreen(wx.Window):
  """Display the graph screen X and Y axis, and create a graph display."""
  def __init__(self, parent, data_width, graph_width, correct_width, correct_delta, in_splitter=0):
    wx.Window.__init__(self, parent, pos = (0, 0))
    self.in_splitter = in_splitter	# Are we in the top of a splitter window?
    self.data_width = data_width
    self.graph_width = graph_width
    self.correct_width = correct_width
    self.correct_delta = correct_delta
    self.doResize = False
    self.pen_tick = wx.Pen("Black", 1, wx.SOLID)
    self.font = wx.Font(10, wx.FONTFAMILY_SWISS, wx.NORMAL, wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface)
    self.SetFont(self.font)
    w = self.GetCharWidth() * 14 / 10
    h = self.GetCharHeight()
    self.freq_start = 1000000
    self.freq_stop  = 2000000
    self.charx = w
    self.chary = h
    self.mode = ''
    self.calibrate_open = None
    self.calibrate_short = None
    self.calibrate_load = None
    self.data_mag = []
    self.data_phase = []
    self.data_impedance = []
    self.data_reflect = []
    self.data_freq = [0] * data_width
    self.tick = max(2, h * 3 / 10)
    self.originX = w * 5
    self.offsetY = h + self.tick
    self.width = self.originX * 2 + self.graph_width + self.tick
    self.height = application.screen_height * 3 / 10
    self.x0 = self.originX + self.graph_width / 2		# center of graph
    self.originY = 10
    self.num_ticks = 8	# number of Y lines above the X axis
    self.dy_ticks = 10
    # The pixel = slope * value + zero_pixel
    # The value = (pixel - zero_pixel) / slope
    self.leftZero = 10		# y location of left zero value
    self.rightZero = 10		# y location of right zero value
    self.leftSlope = 10		# slope of left scale times 360
    self.rightSlope = 10	# slope of right scale times 360
    self.SetSize((self.width, self.height))
    self.SetSizeHints(self.width, 1, self.width)
    self.SetBackgroundColour(conf.color_graph)
    self.Bind(wx.EVT_SIZE, self.OnSize)
    self.Bind(wx.EVT_PAINT, self.OnPaint)
    self.Bind(wx.EVT_LEFT_DOWN, self.OnLeftDown)
    self.Bind(wx.EVT_LEFT_UP, self.OnLeftUp)
    self.Bind(wx.EVT_MOTION, self.OnMotion)
    self.Bind(wx.EVT_MOUSEWHEEL, self.OnWheel)
    self.display = GraphDisplay(self, self.originX, 0, self.graph_width, 5, self.chary)
  def OnPaint(self, event):
    dc = wx.PaintDC(self)
    if not self.in_splitter:
      dc.SetFont(self.font)
      self.MakeYTicks(dc)
      self.MakeXTicks(dc)
  def OnIdle(self, event):
    if self.doResize:
      self.ResizeGraph()
  def OnSize(self, event):
    self.doResize = True
    self.ClearGraph()
    event.Skip()
  def ResizeGraph(self):
    """Change the height of the graph.

    Changing the width interactively is not allowed.
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
    self.doResize = False
    self.Refresh()
  def MakeYScale(self):
    chary = self.chary
    dy = self.dy_ticks = (self.originY - chary * 2) / self.num_ticks   # pixels per tick
    ytot = dy * self.num_ticks
    # Voltage dB scale
    dbs = 80	# Number of dB to display
    self.leftZero = self.originY - ytot - chary
    self.leftSlope = - ytot * 360 / dbs		# pixels per dB times 360
    # Phase scale
    self.rightSlope = - ytot			# pixels per degree times 360
    self.rightZero = self.originY - ytot / 2 - chary
    # SWR scale
    swrs = 9		# display range 1.0 to swrs
    self.swrSlope = - ytot * 360 / (swrs - 1)	# pixels per SWR unit times 360
    self.swrZero = self.originY - self.swrSlope / 360 - chary
  def MakeYTicks(self, dc):
    charx = self.charx
    chary = self.chary
    x1 = self.originX - self.tick * 3	# left of tick mark
    x2 = self.originX - 1		# x location of left y axis
    x3 = self.originX + self.graph_width	# end of graph data
    x4 = x3 + 1				# right y axis
    x5 = x3 + self.tick * 3		# right tick mark
    dc.SetPen(self.pen_tick)
    dc.DrawLine(x2, 0, x2, self.originY + 1)	# y axis
    dc.DrawLine(x4, 0, x4, self.originY + 1)	# y axis
    y = self.leftZero
    del self.display.y_ticks[:]
    for i in range(self.num_ticks + 1):
      # Create the dB scale
      val = (y - self.leftZero) * 360 / self.leftSlope
      t = str(val)
      dc.DrawLine(x1, y, x2, y)
      self.display.y_ticks.append(y)
      w, h = dc.GetTextExtent(t)
      dc.DrawText(t, x1 - w, y - h / 2)
      # Create the scale on the right
      val = (y - self.rightZero) * 360 / self.rightSlope
      t = str(val)
      dc.DrawLine(x4, y, x5, y)
      w, h = dc.GetTextExtent(t)
      dc.DrawText(t, self.width - w - charx, y - h / 2 + 3)	# right text
      y += self.dy_ticks
    # Create the SWR scale
    if self.mode == 'Refl':
      y = self.leftZero
      dc.SetTextForeground(self.display.swrPen.GetColour())
      for i in range(self.num_ticks + 1):
        val = (y - self.swrZero) * 360 / self.swrSlope
        t = str(val)
        w, h = dc.GetTextExtent(t)
        dc.DrawText(t, w/2, y - h / 2)
        y += self.dy_ticks
  def MakeXTicks(self, dc):
    originY = self.originY
    x3 = self.originX + self.graph_width	# end of fft data
    charx , z = dc.GetTextExtent('-30000XX')
    tick0 = self.tick
    tick1 = tick0 * 2
    tick2 = tick0 * 3
    # Draw the X axis
    dc.SetPen(self.pen_tick)
    dc.DrawLine(self.originX, originY, x3, originY)
    sample_rate = int(self.freq_stop - self.freq_start)
    if sample_rate < 12000:
      return
    VFO = int((self.freq_start + self.freq_stop) / 2)
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
    stick =  1000		# small tick in Hertz
    mtick =  5000		# medium tick
    ltick = 10000		# large tick
    # check the width of the frequency label versus frequency span
    df = float(charx) * sample_rate / self.data_width	# max label freq in Hertz
    df *= 2.0
    df = math.log10(df)
    expn = int(df)
    mant = df - expn
    if mant < 0.3:	# label every 10
      tfreq = 10 ** expn
      ltick = tfreq
      mtick = ltick / 2
      stick = ltick / 10
    elif mant < 0.69:	# label every 20
      tfreq = 2 * 10 ** expn
      ltick = tfreq / 2
      mtick = ltick / 2
      stick = ltick / 10
    else:		# label every 50
      tfreq = 5 * 10 ** expn
      ltick = tfreq
      mtick = ltick / 5
      stick = ltick / 10
    # Draw the X axis ticks and frequency in kHz
    dc.SetPen(self.pen_tick)
    freq1 = VFO - sample_rate / 2
    freq1 = (freq1 / stick) * stick
    freq2 = freq1 + sample_rate + stick + 1
    y_end = 0
    for f in range (freq1, freq2, stick):
      x = self.x0 + int(float(f - VFO) / sample_rate * self.data_width)
      if self.originX <= x <= x3:
        if f % ltick is 0:		# large tick
          dc.DrawLine(x, originY, x, originY + tick2)
        elif f % mtick is 0:	# medium tick
          dc.DrawLine(x, originY, x, originY + tick1)
        else:					# small tick
          dc.DrawLine(x, originY, x, originY + tick0)
        if f % tfreq is 0:		# place frequency label
          t = str(f/1000)
          w, h = dc.GetTextExtent(t)
          dc.DrawText(t, x - w / 2, originY + tick2)
          y_end = originY + tick2 + h
    if y_end:		# mark the center of the display
      dc.DrawLine(self.x0, y_end, self.x0, application.screen_height)
  def ClearGraph(self):
    del self.display.line_mag[:]
    del self.display.line_phase[:]
    del self.display.line_swr[:]
    del self.data_mag[:]
    del self.data_phase[:]
    del self.data_impedance[:]
    del self.data_reflect[:]
    self.display.Refresh()
  def SetMode(self, mode):
    self.mode = mode
    if mode == 'Cal Remove':
      self.calibrate_open = None
      self.calibrate_short = None
      self.calibrate_load = None
      self.ClearGraph()
  def SetCorrections(self):
    if self.calibrate_short is not None and self.calibrate_open is not None:
      v8 = []
      for i in range(self.correct_width):
        v8.append((self.calibrate_short[i] - self.calibrate_open[i]) / 2)
      openn = self.calibrate_open[:]
      short = self.calibrate_short[:]
    elif self.calibrate_short is not None:
      v8 = self.calibrate_short[:]
      short = v8
      openn = [0] * self.correct_width
    elif self.calibrate_open is not None:
      v8 = []
      for i in range(self.correct_width):
        v8.append( - self.calibrate_open[i])
      openn = self.calibrate_open[:]
      short = [1] * self.correct_width
    else:
      return
    v8.append(v8[-1])
    openn.append(openn[-1])
    short.append(short[-1])
    delta = self.correct_delta
    self.correct_v8 = []
    self.correct_open = []
    self.correct_short = []
    for x in range(self.data_width):
      # Find the frequency for this pixel
      freq = self.data_freq[x]
      # Find the corresponding index into the correction array
      i = int(freq / delta)
      if i >= self.correct_width:
        i = self.correct_width - 1
      # linear interpolation
      dd = float(freq - i * delta) / delta
      c = v8[i] + (v8[i+1] - v8[i]) * dd
      self.correct_v8.append(c)
      c = openn[i] + (openn[i+1] - openn[i]) * dd
      self.correct_open.append(c)
      c = short[i] + (short[i+1] - short[i]) * dd
      self.correct_short.append(c)
    if DEBUG and self.calibrate_short is not None and self.calibrate_open is not None and self.calibrate_load is not None:
      for index in range(500, 3500, 500):
         freq = delta * index
         Vs = self.calibrate_short[index]
         Vo = self.calibrate_open[index]
         Vl = self.calibrate_load[index]
         Zs = 25 * (Vs - Vl) / Vl
         Zd = 50.0 * ( -5 * Vo * Zs - 55 * Vo + 150 * Vs - 3 * Vs * Zs) / (
               250 * Vo + 3 * Zs * Vo - 250 * Vs + 5 * Vs * Zs)
         #Zx = 250.0 * Zs * (Zd + 30) * (Vs - Vx) / (
         #            5 * Vs * (Zd + 30) * (Zs - 50) + Vx * (250 * (Zs + Zd + 30) + 3 * Zs * Zd))
         print('freq', freq * 1E-6)
         print('Zs', Zs)
         print('Zd', Zd)
         #print 'Zx', Zx
  def OnGraphData(self, volts):
    # Z = 50 * (V8 - v) / (V8 + v)
    # v = V8 * (50 - z) / (50 + z)
    # SWR = (1 + rho) / (1 - rho)
    # Create graph lines
    mode = self.mode
    del self.display.line_mag[:]
    del self.display.line_phase[:]
    del self.display.line_swr[:]
    del self.data_mag[:]
    del self.data_phase[:]
    del self.data_impedance[:]
    del self.data_reflect[:]
    if mode == 'Cal Open':
      self.calibrate_open = volts[:]
    elif mode == 'Cal Short':
      self.calibrate_short = volts[:]
    elif mode == 'Cal Load':
      self.calibrate_load = volts[:]
    use_load = self.calibrate_short is not None and self.calibrate_open is not None and self.calibrate_load is not None
    if mode[0:4] == 'Cal ':
      for x in range(self.graph_width):
        self.data_impedance.append(50)
        self.data_reflect.append(0)
        i = x * self.correct_width / self.data_width
        magn = abs(volts[i])
        phase = cmath.phase(volts[i]) * 360. / (2.0 * math.pi)
        if magn < 1e-6:
          db = -120.0
        else:
          db = 20.0 * math.log10(magn)
        self.data_mag.append(db)
        y = self.leftZero - int( - db * self.leftSlope / 360.0 + 0.5)
        self.display.line_mag.append((x, y))
        self.data_phase.append(phase)
        y = self.rightZero - int( - phase * self.rightSlope / 360.0 + 0.5)
        y = int(y)
        self.display.line_phase.append(y)
    elif mode == 'Refl':
      for x in range(self.graph_width):
        if use_load:		# Use a fancy correction baed on the load calibration
          delta = self.correct_delta
          # Find the frequency for this pixel
          freq = self.data_freq[x]
          # Find the corresponding index into the correction array
          i = int(freq / delta)
          if i >= self.correct_width:
            i = self.correct_width - 1
          Vx = volts[x]
          # linear interpolation
          dd = float(freq - i * delta) / delta
          Vs = self.calibrate_short[i] + (self.calibrate_short[i+1] - self.calibrate_short[i]) * dd
          Vo = self.calibrate_open[i] + (self.calibrate_open[i+1] - self.calibrate_open[i]) * dd
          Vl = self.calibrate_load[i] + (self.calibrate_load[i+1] - self.calibrate_load[i]) * dd
          try:
            Zs = 25 * (Vs - Vl) / Vl
            Zd = 50.0 * ( -5 * Vo * Zs - 55 * Vo + 150 * Vs - 3 * Vs * Zs) / (
                 250 * Vo + 3 * Zs * Vo - 250 * Vs + 5 * Vs * Zs)
            Z  = 250.0 * Zs * (Zd + 30) * (Vs - Vx) / (
                        5 * Vs * (Zd + 30) * (Zs - 50) + Vx * (250 * (Zs + Zd + 30) + 3 * Zs * Zd))
            reflect = (Z - 50) / (Z + 50)
          except:
            if DEBUG: traceback.print_exc()
            reflect = volts[x] / self.correct_v8[x]	#  reflection coefficient
            try:
              Z = 50.0 * (self.correct_v8[x] - volts[x]) / (self.correct_v8[x] + volts[x])
            except ZeroDivisionError:
              Z = 50E3
        else:
          reflect = volts[x] / self.correct_v8[x]	#  reflection coefficient
          try:
            Z = 50.0 * (self.correct_v8[x] - volts[x]) / (self.correct_v8[x] + volts[x])
          except ZeroDivisionError:
            Z = 50E3
        self.data_reflect.append(reflect)
        self.data_impedance.append(Z)
        magn = abs(reflect)
        swr = (1.0 + magn) / (1.0 - magn)
        if not 0.999 <= swr <= 99:
          swr = 99.0
        if magn < 1e-6:
          db = -120.0
        else:
          db = 20.0 * math.log10(magn)
        self.data_mag.append(db)
        y = self.leftZero - int( - db * self.leftSlope / 360.0 + 0.5)
        self.display.line_mag.append((x, y))
        phase = cmath.phase(reflect) * 360. / (2.0 * math.pi)
        self.data_phase.append(phase)
        y = self.rightZero - int( - phase * self.rightSlope / 360.0 + 0.5)
        y = int(y)
        self.display.line_phase.append(y)
        y = self.swrZero - int( - swr * self.swrSlope / 360.0 + 0.5)
        self.display.line_swr.append((x,y))
    else:
      for x in range(self.graph_width):
        self.data_impedance.append(50)
        reflect = (volts[x] - self.correct_open[x]) / self.correct_short[x]
        self.data_reflect.append(reflect)
        magn = abs(reflect)
        if magn < 1e-6:
          db = -120.0
        else:
          db = 20.0 * math.log10(magn)
        self.data_mag.append(db)
        y = self.leftZero - int( - db * self.leftSlope / 360.0 + 0.5)
        self.display.line_mag.append((x, y))
        phase = cmath.phase(reflect) * 360. / (2.0 * math.pi)
        self.data_phase.append(phase)
        y = self.rightZero - int( - phase * self.rightSlope / 360.0 + 0.5)
        y = int(y)
        self.display.line_phase.append(y)
    self.display.Refresh()
  def NewFreq(self, start, stop):
    if self.freq_start != start or self.freq_stop != stop:
      self.ClearGraph()
    self.freq_start = start
    self.freq_stop = stop
    for i in range(self.data_width):	# The frequency in Hertz for every pixel
      self.data_freq[i] = int(start + float(stop - start) * i / (self.data_width - 1) + 0.5)
    self.SetCorrections()
    self.SetTxFreq(index=self.display.tune_tx)
    self.doResize = True
  def SetTxFreq(self, freq=None, index=None):
    if index is None:
      index = int(float(freq - self.freq_start) * (self.data_width - 1) / (self.freq_stop - self.freq_start) + 0.5)
    if index < 0:
      index = 0
    elif index >= self.data_width:
      index = self.data_width - 1
    if freq is None:
      freq = self.data_freq[index]
    self.display.SetTuningLine(index)
    application.ShowFreq(freq, index)
  def GetMousePosition(self, event):
    """For mouse clicks in our display, translate to our screen coordinates."""
    mouse_x, mouse_y = event.GetPositionTuple()
    win = event.GetEventObject()
    if win is not self:
      x, y = win.GetPositionTuple()
      mouse_x += x
      mouse_y += y
    return mouse_x, mouse_y
  def OnLeftDown(self, event):
    mouse_x, mouse_y = self.GetMousePosition(event)
    self.SetTxFreq(index=mouse_x - self.originX)
    self.CaptureMouse()
  def OnLeftUp(self, event):
    if self.HasCapture():
      self.ReleaseMouse()
  def OnMotion(self, event):
    if event.Dragging() and event.LeftIsDown():
      mouse_x, mouse_y = self.GetMousePosition(event)
      self.SetTxFreq(index=mouse_x - self.originX)
  def OnWheel(self, event):
    tune = self.display.tune_tx + event.GetWheelRotation() / event.GetWheelDelta()
    self.SetTxFreq(index=tune)

class HelpScreen(wx.html.HtmlWindow):
  """Create the screen for the Help button."""
  def __init__(self, parent, width, height):
    wx.html.HtmlWindow.__init__(self, parent, -1, size=(width, height))
    if "gtk2" in wx.PlatformInfo:
      self.SetStandardFonts()
    self.SetFonts("", "", [10, 12, 14, 16, 18, 20, 22])
    # read in text from file help.html in the directory of this module
    self.LoadFile('help_vna.html')
  def OnLinkClicked(self, link):
    webbrowser.open(link.GetHref(), new=2)

class QMainFrame(wx.Frame):
  """Create the main top-level window."""
  def __init__(self, width, height):
    title = 'Quisk Vector Network Analyzer'
    wx.Frame.__init__(self, None, -1, title, wx.DefaultPosition,
        (width, height), wx.DEFAULT_FRAME_STYLE, 'MainFrame')
    self.SetBackgroundColour(conf.color_bg)
    self.Bind(wx.EVT_CLOSE, self.OnBtnClose)
  def OnBtnClose(self, event):
    application.OnBtnClose(event)
    self.Destroy()

class Spacer(wx.Window):
  """Create a bar between the graph screen and the controls"""
  def __init__(self, parent):
    wx.Window.__init__(self, parent, pos = (0, 0),
       size=(-1, 6), style = wx.NO_BORDER)
    self.Bind(wx.EVT_PAINT, self.OnPaint)
    r, g, b = parent.GetBackgroundColour().Get()
    dark = (r * 7 / 10, g * 7 / 10, b * 7 / 10)
    light = (r + (255 - r) * 5 / 10, g + (255 - g) * 5 / 10, b + (255 - b) * 5 / 10)
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
  StateNames =[]
  def __init__(self):
    global application
    application = self
    self.bottom_widgets = None
    self.is_vna_program = None
    if sys.stdout.isatty():
      wx.App.__init__(self, redirect=False)
    else:
      wx.App.__init__(self, redirect=True)
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
      for k, v in d.items():		# add user's config items to conf
        if k[0] != '_':				# omit items starting with '_'
          setattr(conf, k, v)
    else:
      setattr(conf, 'config_file_exists', False)
    self.graph_freq = 7e6
    self.graph_index = 50
    # Open hardware file
    self.firmware_version = None
    global Hardware
    if hasattr(conf, "Hardware"):	# Hardware defined in config file
      Hardware = conf.Hardware(self, conf)
    else:
      Hardware = conf.quisk_hardware.Hardware(self, conf)
    # Initialization
    # get the screen size
    x, y, self.screen_width, self.screen_height = wx.Display().GetGeometry()
    self.Bind(wx.EVT_QUERY_END_SESSION, self.OnEndSession)
    self.sample_rate = 48000
    self.timer = time.time()		# A seconds clock
    self.time0 = 0			# timer to display fields
    self.clip_time0 = 0			# timer to display a CLIP message on ADC overflow
    self.heart_time0 = self.timer	# timer to call HeartBeat at intervals
    self.status_text = 'Freq '
    self.running = False
    self.save_data = []
    # correct_delta is the spacing of correction points in Hertz
    self.correct_delta = 15000
    self.max_freq = 60000000	# maximum calculation frequency
    self.correct_width = self.max_freq / self.correct_delta + 4		# number of data points in the correct arrays
    # Find the data width, the width of returned graph data.
    width = self.screen_width * conf.graph_width
    width = int(width)
    self.data_width = width
    self.main_frame = frame = QMainFrame(10, 10)
    self.SetTopWindow(frame)
    # Record the basic application parameters
    if sys.platform == 'win32':
      h = self.main_frame.GetHandle()
    else:
      h = 0
    # FFT size must equal the data_width so that all data points are returned!
    QS.record_app(self, conf, self.data_width, self.data_width,
                 1, self.sample_rate, h)
    # Make all the screens and hide all but one
    self.graph = GraphScreen(frame, self.data_width, self.data_width, self.correct_width, self.correct_delta)
    self.screen = self.graph
    width = self.graph.width
    self.help_screen = HelpScreen(frame, width, self.screen_height / 10)
    self.help_screen.Hide()
    # Make a vertical box to hold all the screens and the bottom rows
    vertBox = self.vertBox = wx.BoxSizer(wx.VERTICAL)
    frame.SetSizer(vertBox)
    # Add the screens
    vertBox.Add(self.graph, 1)
    vertBox.Add(self.help_screen, 1)
    # Add the spacer
    vertBox.Add(Spacer(frame), 0, wx.EXPAND)
    # Add the sizers for the buttons
    szr1 = wx.BoxSizer(wx.HORIZONTAL)
    szr2 = wx.BoxSizer(wx.HORIZONTAL)
    vertBox.Add(szr1, 0, wx.EXPAND, 0)
    vertBox.Add(szr2, 0, wx.EXPAND, 0)
    # Make the buttons in row 1
    buttons1 = []
    self.group_screen = RadioButtonGroup(frame, self.OnBtnScreen, ('Trans', 'Refl', 'Help'), 'Refl')
    buttons1 += self.group_screen.buttons
    self.btn_run = b = QuiskCheckbutton(frame, self.OnBtnRun, 'Run')
    buttons1.append(b)
    b = QuiskCheckbutton(frame, self.OnBtnCal, 'Cal Open')
    buttons1.append(b)
    b = QuiskCheckbutton(frame, self.OnBtnCal, 'Cal Short')
    buttons1.append(b)
    b = self.btn_calload = QuiskCheckbutton(frame, self.OnBtnCal, 'Cal Load')
    buttons1.append(b)
    b = QuiskPushbutton(frame, self.OnBtnCal, 'Cal Remove')
    buttons1.append(b)
    width = 0
    for b in buttons1[0:4]:
      w, height = b.GetMinSize()
      if width < w:
        width = w
    for i in range(24, 8, -2):
      font = wx.Font(i, wx.FONTFAMILY_SWISS, wx.NORMAL, wx.FONTWEIGHT_NORMAL, face=conf.quisk_typeface)
      frame.SetFont(font)
      w, h = frame.GetTextExtent('Start   ')
      if h < height * 9 / 10:
        break
    for b in buttons1[0:4]:
      b.SetMinSize((width, height))
    # Text status
    style = wx.ST_NO_AUTORESIZE
    i = 0
    for t in ('No hardware response        ', 'Error in rates        ',
           'Mag -1100.12 dB     ', 'Phase -1800.12     ', 'SWR 555.1     ',
           '0'):
      c = wx.lib.stattext.GenStaticText(frame, -1, t, style=style)
      setattr(self, "t_field%d" % i, c)
      c.SetBackgroundColour(conf.color_bg)
      w, h = c.GetSizeTuple()
      c.SetMinSize((w, -1))
      if i == 5:
        szr2.Add(c, 1, wx.ALIGN_CENTER_VERTICAL)
      else:
        szr2.Add(c, 0, wx.ALIGN_CENTER_VERTICAL)
      i += 1
    # Frequency entry start and stop
    t = wx.lib.stattext.GenStaticText(frame, -1, 'Start  ')
    t.SetFont(font)
    t.SetBackgroundColour(conf.color_bg)
    gap = max(2, height/8)
    freq0 = t
    e = wx.TextCtrl(frame, -1, '1', style=wx.TE_PROCESS_ENTER)
    e.SetFont(font)
    w, h = e.GetSizeTuple()
    e.SetMinSize((-1, height))
    e.SetBackgroundColour(conf.color_entry)
    self.freq_start_ctrl = e
    frame.Bind(wx.EVT_TEXT_ENTER, self.OnNewFreq, source=e)
    frame.Bind(wx.EVT_TEXT, self.OnNewFreq, source=e)
    t = wx.lib.stattext.GenStaticText(frame, -1, 'Stop  ')
    t.SetFont(font)
    t.SetBackgroundColour(conf.color_bg)
    freq2 = t
    e = wx.TextCtrl(frame, -1, '30', style=wx.TE_PROCESS_ENTER)
    e.SetFont(font)
    e.SetMinSize((-1, height))
    e.SetBackgroundColour(conf.color_entry)
    self.freq_stop_ctrl = e
    frame.Bind(wx.EVT_TEXT_ENTER, self.OnNewFreq, source=e)
    frame.Bind(wx.EVT_TEXT, self.OnNewFreq, source=e)
    # Band buttons
    ilst = []
    slst = []
    for l in conf.BandEdge.keys():	# Sort keys
      try:
        ilst.append((int(l), conf.BandEdge[l]))
      except ValueError:	# item is a string, not an integer
        slst.append((l, conf.BandEdge[l]))
    ilst.sort()
    ilst.reverse()
    slst.sort()
    band = []
    width = 0
    for l in ilst + slst:
      b = QuiskPushbutton(frame, self.OnBtnBand, str(l[0]))
      b.bandEdge = l[1]
      band.append(b)
      w, h= b.GetMinSize()
      if width < w:
        width = w
    #for b in band:
    #  b.SetMinSize((width, height))
    # make a list of all buttons
    self.buttons = buttons1 + band
    # Add first button row to sizer
    gap = max(2, height / 8)
    gap2 = max(2, height / 4)
    szr1.Add(buttons1[0], 0, wx.RIGHT|wx.LEFT, gap)
    szr1.Add(buttons1[1], 0, wx.RIGHT, gap)
    szr1.Add(buttons1[2], 0, wx.RIGHT, gap)
    szr1.Add(buttons1[3], 0, wx.RIGHT|wx.LEFT, gap2)
    szr1.Add(buttons1[4], 0, wx.RIGHT|wx.LEFT, gap)
    szr1.Add(buttons1[5], 0, wx.RIGHT, gap)
    szr1.Add(buttons1[6], 0, wx.RIGHT, gap)
    szr1.Add(buttons1[7], 0, wx.RIGHT, gap)
    szr1.Add(freq0, 0, wx.ALIGN_CENTER_VERTICAL)
    szr1.Add(self.freq_start_ctrl, 1, wx.RIGHT, gap)
    szr1.Add(freq2, 0, wx.ALIGN_CENTER_VERTICAL)
    szr1.Add(self.freq_stop_ctrl, 1, wx.RIGHT, gap)
    for x in band:
      szr1.Add(x, 0, wx.RIGHT, gap)
    # Set top window size
    self.main_frame.SetClientSizeWH(self.graph.width, self.screen_height * 5 / 10)
    # Open the hardware.  This must be called before open_sound().
    self.config_text = Hardware.open()
    if not self.config_text:
      self.config_text = "Missing config_text"
    if hasattr(Hardware, 'SetVNA'):
      self.has_SetVNA = True
    else:
      self.has_SetVNA = False
    # Note: Subsequent calls to set channels must not name a higher channel number.
    #       Normally, these calls are only used to reverse the channels.
    QS.open_sound(conf.name_of_sound_capt, '', self.sample_rate,
                conf.data_poll_usec, conf.latency_millisecs,
                '', conf.tx_ip, conf.tx_audio_port,
                48000, 0, 0, 1.0, '', 48000)
    self.Bind(wx.EVT_IDLE, self.graph.OnIdle)
    frame.Show()
    self.Yield()
    if self.has_SetVNA:
      Hardware.SetVNA(key_down=0, vna_count=self.data_width)
    self.OnNewFreq()
    self.WriteFields()
    self.EnableButtons()
    QS.set_fdx(1)
    QS.set_rx_mode(0)
    self.sound_thread = SoundThread()
    self.sound_thread.start()
    return True
  def OnEndSession(self, event):
    event.Skip()
    self.OnBtnClose(event)
  def OnBtnClose(self, event):
    if self.sound_thread:
      self.sound_thread.stop()
    for i in range(0, 20):
      if threading.activeCount() == 1:
        break
      time.sleep(0.1)
  def OnBtnBand(self, event):
    btn = event.GetEventObject()
    start, stop = btn.bandEdge
    start = float(start) * 1e-6
    stop = float(stop) * 1e-6
    self.freq_start_ctrl.SetValue(str(start))
    self.freq_stop_ctrl.SetValue(str(stop))
  def OnBtnCal(self, event):
    btn = event.GetEventObject()
    mode = btn.GetLabel()
    self.btn_run.SetValue(0)
    self.graph.SetMode(mode)
    if mode == 'Cal Remove':
      self.running = False
      self.btn_run.Enable(0)
      self.graph.SetCorrections()
      self.SetField0()
    elif btn.GetValue():
      for b in self.buttons:
        if b != btn:
          b.Enable(0)
      self.btn_run.Enable(0)
      self.NewFreq(0, self.max_freq)
      if self.has_SetVNA:
        count = self.correct_width
        max_freq = self.correct_delta * count
        Hardware.SetVNA(key_down=1, vna_start=0, vna_stop=max_freq, vna_count=count)
      self.running = True
    else:
      self.running = False
      for b in self.buttons:
        b.Enable(1)
      self.graph.SetCorrections()
      self.SetField0()
      if self.has_SetVNA:
        Hardware.SetVNA(key_down=0, vna_count=self.data_width)
      self.EnableButtons()
  def OnBtnScreen(self, event):
    btn = event.GetEventObject()
    screen = btn.GetLabel()
    if screen == 'Help':
      self.help_screen.Show()
      self.graph.Hide()
    else:
      self.help_screen.Hide()
      self.graph.Show()
      self.graph.SetMode(screen)
    self.vertBox.Layout()
    self.EnableButtons()
  def OnBtnRun(self, event):
    btn = event.GetEventObject()
    run = btn.GetValue()
    self.graph.SetMode(self.group_screen.GetLabel())
    self.OnNewFreq()
    if self.has_SetVNA:
      if run:
        self.running = True
        Hardware.SetVNA(key_down=1)
      else:
        self.running = False
        Hardware.SetVNA(key_down=0)
  def EnableButtons(self):
    mode = self.group_screen.GetLabel()
    if mode == 'Trans':
      self.btn_calload.Enable(0)
      if self.graph.calibrate_short is not None:
        self.btn_run.Enable(1)
      else:
        self.btn_run.Enable(0)
    else:		# Refl
      self.btn_calload.Enable(1)
      if self.graph.calibrate_short is not None or self.graph.calibrate_open is not None:
        self.btn_run.Enable(1)
      else:
        self.btn_run.Enable(0)
  def ShowFreq(self, freq, index):
    if hasattr(Hardware, 'ChangeFilterFrequency'):
      Hardware.ChangeFilterFrequency(freq)
    self.graph_freq = freq
    self.graph_index = index
    self.status_text = "Freq %9.6f" % (freq * 1.E-6)
    if self.t_field1.GetLabel()[0:5] == 'Freq ':
      self.t_field1.SetLabel(self.status_text)
    self.WriteFields()
  def OnNewFreq(self, event=None):
    try:
      start = self.freq_start_ctrl.GetValue()
      start = float(start) * 1e6
      stop = self.freq_stop_ctrl.GetValue()
      stop = float(stop) * 1e6
    except:
      self.t_field1.SetLabel("Error in rates")
      #traceback.print_exc()
      return
    start = int(start + 0.5)
    stop = int(stop + 0.5)
    if start > stop:
      self.t_field1.SetLabel("Error in rates")
      return
    if stop > self.max_freq:
      stop = self.max_freq
      self.freq_stop_ctrl.SetValue("%.6f" % (stop * 1.E-6))
    self.NewFreq(start, stop)
  def NewFreq(self, start, stop):
    if application.has_SetVNA:
      start, stop = Hardware.SetVNA(vna_start=start, vna_stop=stop)
    self.freq_start = start
    self.freq_stop = stop
    self.t_field1.SetLabel(self.status_text)
    self.graph.NewFreq(start, stop)
  def SetField0(self):
    if self.firmware_version is None:
      text = "No hardware response"
    elif self.firmware_version < 3:
      text = "Need firmware ver 3 "
    else:
      text = ''
      if self.graph.calibrate_open is not None:
        text = text + 'Op'
      if self.graph.calibrate_short is not None:
        text = text + 'Sh'
      if self.graph.calibrate_load is not None:
        text = text + 'Lo'
      if text:
        text = 'Calibration ' + text
      else:
        text = 'Calibration None'
    self.t_field0.SetLabel(text)
  def WriteFields(self):
    if not self.graph.data_mag:
      self.t_field2.SetLabel('')
      self.t_field3.SetLabel('')
      self.t_field4.SetLabel('')
      self.t_field5.SetLabel('')
      return
    index = self.graph_index
    if index < 0:
      index = 0
    elif index >= self.data_width:
      index = self.data_width - 1
    mode = self.graph.mode
    if mode in ('Cal Open', 'Cal Short', ''):
      db = self.graph.data_mag[index]
      phase = self.graph.data_phase[index]
      self.t_field2.SetLabel('Mag %7.2f dB' % db)
      self.t_field3.SetLabel('Phase %6.1f' % phase)
      self.t_field4.SetLabel('')
      self.t_field5.SetLabel('')
    elif mode == 'Trans':
      db = self.graph.data_mag[index]
      phase = self.graph.data_phase[index]
      aref = abs(self.graph.data_reflect[index])
      self.t_field2.SetLabel('Mag %8.2f dB' % db)
      self.t_field3.SetLabel('Phase %8.2f' % phase)
      self.t_field4.SetLabel('')
      self.t_field5.SetLabel('')
    elif mode == 'Refl':
      db = self.graph.data_mag[index]
      phase = self.graph.data_phase[index]
      aref = abs(self.graph.data_reflect[index])
      swr = (1.0 + aref) / (1.0 - aref)
      if not 0.999 <= swr <= 99:
        swr = 99.0
      self.t_field2.SetLabel('Mag %7.2f dB' % db)
      self.t_field3.SetLabel('Phase %6.1f' % phase)
      self.t_field4.SetLabel('SWR %6.1f' % swr)
      Z = self.graph.data_impedance[index]
      mag = abs(Z)
      phase = cmath.phase(Z) * 360. / (2.0 * math.pi)
      freq = self.graph.data_freq[index]
      im = Z.imag
      text = '    Imped %8.1f  %8.1f J    Mag %7.2f  Phase %6.1f' % (Z.real, im, mag, phase)
      if im >= 0.5:
        L = im / (2.0 * math.pi * freq) * 1e9
        text += '    L %10.0f nH' % L
      elif im < -0.5:
        C = -1.0 / (2.0 * math.pi * freq * im) * 1e9
        text += '    C %10.3f nF' % C
      self.t_field5.SetLabel(text)
  def OnExit(self):
    if self.has_SetVNA:
      Hardware.SetVNA(key_down=0, do_tx=True)
    time.sleep(0.5)
    QS.close_rx_udp()
    Hardware.close()
  def PostStartup(self):	# called once after sound attempts to start
    pass
  def OnReadSound(self):	# called at frequent intervals
    self.timer = time.time()
    dat = QS.get_graph(0, 1.0, 0)
    if dat and self.running:
      dat = list(dat)
      try:
        start = dat.index(0)
      except ValueError:
        self.save_data += dat
        return
      data = self.save_data + dat[0:start]
      self.save_data = dat[start+1:]
      if self.graph.mode[0:3] == 'Cal':
        if len(data) != self.correct_width:
          if DEBUG: print('  bad data array', len(data), self.correct_width)
          return
      else:
        if len(data) != self.data_width:
          if DEBUG: print('  bad data array', len(data), self.data_width)
          return
      #print "%10.2f  %12.8f %10.2f %10.2f %10.2f" % (cmath.phase(data[0]) * 360. / (2.0 * math.pi), abs(data[0]) / 2147483647.0,
      # cmath.phase(data[0]) * 360. / (2.0 * math.pi), cmath.phase(data[0]) * 360. / (2.0 * math.pi), cmath.phase(data[0]) * 360. / (2.0 * math.pi))
      for i in range(len(data)):
        data[i] /= 2147483647.0
      self.graph.OnGraphData(data)
    if QS.get_overrange():
      self.clip_time0 = self.timer
      self.t_field1.SetLabel("CLIP")
    if self.clip_time0:
      if self.timer - self.clip_time0 > 1.0:
        self.clip_time0 = 0
        self.t_field1.SetLabel(self.status_text)
    if self.timer - self.heart_time0 > 0.10:		# call hardware to perform background tasks
      self.heart_time0 = self.timer
      Hardware.HeartBeat()
      if self.firmware_version is None and conf.use_rx_udp:
        self.firmware_version = Hardware.GetFirmwareVersion()
        self.SetField0()
    # Set text fields
    if  self.timer - self.time0 > 0.5:
      self.time0 = self.timer
      #print "len %5d  re %9.6f  im %9.6f  mag %9.6f  phase %7.2f" % (len(data),
      #   volts.real, volts.imag, abs(volts), phase)
      #print "Z re %12.2f  im %12.2f  mag %12.2f  phase %7.2f" % (zzz.real, zzz.imag,
      #  abs(zzz), cmath.phase(zzz) * 360. / (2.0 * math.pi))
      self.WriteFields()

def main():
  """If quisk is installed as a package, you can run it with quisk.main()."""
  App()
  application.MainLoop()

if __name__ == '__main__':
  main()

