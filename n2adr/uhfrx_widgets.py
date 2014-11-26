# Please do not change this widgets module for Quisk.  Instead copy
# it to your own quisk_widgets.py and make changes there.
#
# This module is used to add extra widgets to the QUISK screen.

from __future__ import print_function

import wx, time
import _quisk as QS

class BottomWidgets:	# Add extra widgets to the bottom of the screen
  def __init__(self, app, hardware, conf, frame, gbs, vertBox):
    self.config = conf
    self.hardware = hardware
    self.application = app
    row = 4			# The next available row
    b = app.QuiskPushbutton(frame, hardware.TestVfoPlus, 'Adf+')
    bw, bh = b.GetMinSize()
    gbs.Add(b, (row, 0), flag=wx.EXPAND)
    b = app.QuiskPushbutton(frame, hardware.TestVfoMinus, 'Adf-')
    gbs.Add(b, (row, 1), flag=wx.EXPAND)
    self.status_label = app.QuiskText(frame, 'Ready', bh)
    gbs.Add(self.status_label, (row, 2), (1, 15), flag=wx.EXPAND)
  def UpdateText(self, text):
    self.status_label.SetLabel(text)

