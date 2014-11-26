# Please do not change this hardware control module for Quisk.
# It provides USB control of SoftRock hardware.

from __future__ import print_function

import struct, threading, time, traceback, math
from quisk_hardware_model import Hardware as BaseHardware
import _quisk as QS

# All USB access is through control transfers using pyusb.
#   byte_array      = dev.ctrl_transfer (IN,  bmRequest, wValue, wIndex, length, timout)
#   len(string_msg) = dev.ctrl_transfer (OUT, bmRequest, wValue, wIndex, string_msg, timout)

import usb.core, usb.util

DEBUG = 0

# Thanks to Ethan Blanton, KB8OJH, for this patch for the Si570 (many SoftRocks):
# These are used by SetFreqByDirect(); see below.
# The Si570 DCO must be clamped between these values
SI570_MIN_DCO = 4.85e9
SI570_MAX_DCO = 5.67e9
# The Si570 has 6 valid HSDIV values.  Subtract 4 from HSDIV before
# stuffing it.  We want to find the highest HSDIV first, so start
# from 11.
SI570_HSDIV_VALUES = [11, 9, 7, 6, 5, 4]

IN =  usb.util.build_request_type(usb.util.CTRL_IN,  usb.util.CTRL_TYPE_VENDOR, usb.util.CTRL_RECIPIENT_DEVICE)
OUT = usb.util.build_request_type(usb.util.CTRL_OUT, usb.util.CTRL_TYPE_VENDOR, usb.util.CTRL_RECIPIENT_DEVICE)

UBYTE2 = struct.Struct('<H')
UBYTE4 = struct.Struct('<L')	# Thanks to Sivan Toledo

class Hardware(BaseHardware):
  def __init__(self, app, conf):
    BaseHardware.__init__(self, app, conf)
    self.usb_dev = None
    self.vfo = None
    self.ptt_button = 0
    self.is_cw = False
    self.key_thread = None
    self.si570_i2c_address = conf.si570_i2c_address
  def open(self):			# Called once to open the Hardware
    # find our device
    usb_dev = usb.core.find(idVendor=self.conf.usb_vendor_id, idProduct=self.conf.usb_product_id)
    if usb_dev is None:
      text = 'USB device not found VendorID 0x%X ProductID 0x%X' % (
          self.conf.usb_vendor_id, self.conf.usb_product_id)
      self.application.sound_error = 1
    else:
      try:		# This exception occurs for the Peabody SDR.  Thanks to ON7VQ for figuring out the problem,
        usb_dev.set_configuration()        # and to David, AE9RB, for the fix.
      except:
        if DEBUG: traceback.print_exc()
      try:
        ret = usb_dev.ctrl_transfer(IN, 0x00, 0x0E00, 0, 2)
      except:
        if DEBUG: traceback.print_exc()
        text = "No permission to access the SoftRock USB interface"
        self.application.sound_error = 1
      else:
        self.usb_dev = usb_dev		# success
        if len(ret) == 2:
          ver = "%d.%d" % (ret[1], ret[0])
        else:
          ver = 'unknown'
        text = 'Capture from SoftRock USB on %s, Firmware %s' % (self.conf.name_of_sound_capt , ver)
        if self.conf.name_of_mic_play and self.conf.key_poll_msec:
          self.key_thread = KeyThread(usb_dev, self.conf.key_poll_msec / 1000.0, self.conf.key_hang_time)
          self.key_thread.start()
    self.application.bottom_widgets.info_text.SetLabel(text)
    if DEBUG:
      print ('Startup freq', self.GetStartupFreq())
      print ('Run freq', self.GetFreq())
      print ('Address 0x%X' % usb_dev.ctrl_transfer(IN, 0x41, 0, 0, 1)[0])
      sm = usb_dev.ctrl_transfer(IN, 0x3B, 0, 0, 2)
      sm = UBYTE2.unpack(sm)[0]
      print ('Smooth tune', sm)
    return text
  def close(self):			# Called once to close the Hardware
    if self.key_thread:
      self.key_thread.stop()
      self.key_thread = None
  def ChangeFrequency(self, tune, vfo, source='', band='', event=None):
    if self.usb_dev and self.vfo != vfo:
      if self.conf.si570_direct_control:
        if self.SetFreqByDirect(vfo):
          self.vfo = vfo
      elif self.SetFreqByValue(vfo):
         self.vfo = vfo
      if DEBUG:
        print ('Change to', vfo)
        print ('Run freq', self.GetFreq())
    return tune, vfo
  def ReturnFrequency(self):
    # Return the current tuning and VFO frequency.  If neither have changed,
    # you can return (None, None).  This is called at about 10 Hz by the main.
    # return (tune, vfo)	# return changed frequencies
    return None, None		# frequencies have not changed
  def ChangeMode(self, mode):		# Change the tx/rx mode
    # mode is a string: "USB", "AM", etc.
    if mode in ('CWU', 'CWL'):
      self.is_cw = True
    else:
      self.is_cw = False
    if self.key_thread:
      self.key_thread.IsCW(self.is_cw)
    elif hasattr(self, 'OnButtonPTT'):
      self.OnButtonPTT()
  def ChangeBand(self, band):
    # band is a string: "60", "40", "WWV", etc.
    pass
  def OnSpot(self, level):
    if self.key_thread:
      self.key_thread.OnSpot(level)
  def HeartBeat(self):	# Called at about 10 Hz by the main
    pass
  def OnButtonPTT(self, event=None):
    if event:
      if event.GetEventObject().GetValue():
        self.ptt_button = 1
      else:
        self.ptt_button = 0
    if self.key_thread:
      self.key_thread.OnPTT(self.ptt_button)
    elif self.usb_dev:
      if self.is_cw:
        QS.set_key_down(0)
        QS.set_transmit_mode(self.ptt_button)
      else:
        QS.set_key_down(self.ptt_button)
      try:
        self.usb_dev.ctrl_transfer(IN, 0x50, self.ptt_button, 0, 3)
      except usb.core.USBError:
        if DEBUG: traceback.print_exc()
        try:
          self.usb_dev.ctrl_transfer(IN, 0x50, self.ptt_button, 0, 3)
        except usb.core.USBError:
          if DEBUG: traceback.print_exc()
  def GetStartupFreq(self):	# return the startup frequency / 4
    if not self.usb_dev:
      return 0
    ret = self.usb_dev.ctrl_transfer(IN, 0x3C, 0, 0, 4)
    s = ret.tostring()
    freq = UBYTE4.unpack(s)[0]
    freq = int(freq * 1.0e6 / 2097152.0 / 4.0 + 0.5)
    return freq
  def GetFreq(self):	# return the running frequency / 4
    if not self.usb_dev:
      return 0
    ret = self.usb_dev.ctrl_transfer(IN, 0x3A, 0, 0, 4)
    s = ret.tostring()
    freq = UBYTE4.unpack(s)[0]
    freq = int(freq * 1.0e6 / 2097152.0 / 4.0 + 0.5)
    return freq
  def SetFreqByValue(self, freq):
    freq = int(freq/1.0e6 * 2097152.0 * 4.0 + 0.5)
    if freq <= 0:
      return
    s = UBYTE4.pack(freq)
    try:
      self.usb_dev.ctrl_transfer(OUT, 0x32, self.si570_i2c_address + 0x700, 0, s)
    except usb.core.USBError:
      if DEBUG: traceback.print_exc()
    else:
      return True
  def SetFreqByDirect(self, freq):	# Thanks to Ethan Blanton, KB8OJH
    if freq == 0.0:
      return False
    # For now, find the minimum DCO speed that will give us the
    # desired frequency; if we're slewing in the future, we want this
    # to additionally yield an RFREQ ~= 512.
    freq = int(freq * 4)
    dco_new = None
    hsdiv_new = 0
    n1_new = 0
    for hsdiv in SI570_HSDIV_VALUES:
      n1 = int(math.ceil(SI570_MIN_DCO / (freq * hsdiv)))
      if n1 < 1:
        n1 = 1
      else:
        n1 = ((n1 + 1) / 2) * 2
      dco = (freq * 1.0) * hsdiv * n1
      # Since we're starting with max hsdiv, this can only happen if
      # freq was larger than we can handle
      if n1 > 128:
        continue
      if dco < SI570_MIN_DCO or dco > SI570_MAX_DCO:
        # This really shouldn't happen
        continue
      if not dco_new or dco < dco_new:
        dco_new = dco
        hsdiv_new = hsdiv
        n1_new = n1
    if not dco_new:
      # For some reason, we were unable to calculate a frequency.
      # Probably because the frequency requested is outside the range
      # of our device.
      return False		# Failure
    rfreq = dco_new / self.conf.si570_xtal_freq
    rfreq_int = int(rfreq)
    rfreq_frac = int(round((rfreq - rfreq_int) * 2**28))
    # It looks like the DG8SAQ protocol just passes r7-r12 straight
    # To the Si570 when given command 0x30.  Easy enough.
    # n1 is stuffed as n1 - 1, hsdiv is stuffed as hsdiv - 4.
    hsdiv_new = hsdiv_new - 4
    n1_new = n1_new - 1
    s = struct.Struct('>BBL').pack((hsdiv_new << 5) + (n1_new >> 2),
                                   ((n1_new & 0x3) << 6) + (rfreq_int >> 4),
                                   ((rfreq_int & 0xf) << 28) + rfreq_frac)
    self.usb_dev.ctrl_transfer(OUT, 0x30, self.si570_i2c_address + 0x700, 0, s)
    return True		# Success

class KeyThread(threading.Thread):
  """Create a thread to monitor the key state."""
  def __init__(self, dev, poll_secs, key_hang_time):
    self.usb_dev = dev
    self.poll_secs = poll_secs
    self.key_hang_time = key_hang_time
    self.ptt_button = 0
    self.spot_level = 0
    self.currently_in_tx = 0
    self.is_cw = False
    self.key_timer = 0
    self.is_transmit = 0
    threading.Thread.__init__(self)
    self.doQuit = threading.Event()
    self.doQuit.clear()
  def run(self):
    while not self.doQuit.isSet():
      if self.is_cw:
        try:		# Test key up/down state
          ret = self.usb_dev.ctrl_transfer(IN, 0x51, 0, 0, 1)
        except usb.core.USBError:
          if DEBUG: traceback.print_exc()
        else:
          # bit 0x20 is the tip, bit 0x02 is the ring (ring not used)
          if self.spot_level or ret[0] & 0x20 == 0:		# Tip: key is down
            QS.set_key_down(1)
            self.is_transmit = 1
            self.key_timer = time.time()
          else:			# key is up
            QS.set_key_down(0)
            if self.is_transmit and time.time() - self.key_timer > self.key_hang_time:
              self.is_transmit = 0
          if self.is_transmit != self.currently_in_tx:
            try:
              self.usb_dev.ctrl_transfer(IN, 0x50, self.is_transmit, 0, 3)
            except usb.core.USBError:
              if DEBUG: traceback.print_exc()
            else:
              self.currently_in_tx = self.is_transmit	# success
              QS.set_transmit_mode(self.is_transmit)
              if DEBUG: print ("Change currently_in_tx", self.currently_in_tx)
      elif self.ptt_button != self.currently_in_tx:
        QS.set_key_down(self.ptt_button)
        try:
          self.usb_dev.ctrl_transfer(IN, 0x50, self.ptt_button, 0, 3)
        except usb.core.USBError:
          if DEBUG: traceback.print_exc()
        else:
          self.currently_in_tx = self.ptt_button	# success
          if DEBUG: print ("Change currently_in_tx", self.currently_in_tx)
      time.sleep(self.poll_secs)
  def stop(self):
    """Set a flag to indicate that the thread should end."""
    self.doQuit.set()
  def OnPTT(self, ptt):
    self.ptt_button = ptt
  def OnSpot(self, level):
    self.spot_level = level
  def IsCW(self, is_cw):
    self.is_cw = is_cw
