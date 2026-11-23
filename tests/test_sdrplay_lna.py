import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from types import SimpleNamespace as NS
from andysdr import sdrplay as sp

def mk(hw, centre, gain, ant=""):
    d = sp.SdrplayDevice.__new__(sp.SdrplayDevice)
    d.hw, d.center, d.gain, d.antenna = hw, centre, gain, ant
    rx = NS(tunerParams=NS(gain=NS(gRdB=0, LNAstate=0)))
    d._apply_gain_values(rx)
    return rx.tunerParams.gain

# the failing case from the log: RSP2 Pro, 1088.7 MHz, gain 0 -> must be <= 5, never 8
g = mk(2, 1088.7e6, 0.0)
assert g.LNAstate == 5 and g.gRdB == 59, (g.LNAstate, g.gRdB)
assert mk(2, 145e6, 0.0).LNAstate == 8          # VHF keeps the full range
assert mk(2, 7e6, 0.0, "Hi-Z").LNAstate == 4    # Hi-Z port
assert mk(2, 1088.7e6, 100.0).LNAstate == 0
assert mk(255, 1.5e9, 0.0).LNAstate == 8 and mk(255, 100e6, 0.0).LNAstate == 9
for f in (1e6, 30e6, 100e6, 300e6, 800e6, 1.09e9, 1.9e9):
    for hw in (2, 255, 3, 4):
        assert 0 <= mk(hw, f, 0.0).LNAstate <= sp.SdrplayDevice._lna_max(NS(hw=hw, center=f, antenna=""))
print("lna clamp ok")

# knob acts with AGC on: LNA state follows the RF GAIN knob in both modes
sent = []
d = sp.SdrplayDevice.__new__(sp.SdrplayDevice)
d.hw, d.center, d.gain, d.antenna, d.agc = 2, 153.025e6, 0.0, "", True
d.devT = object()
rx = NS(tunerParams=NS(gain=NS(gRdB=0, LNAstate=0), rfFreq=NS(rfHz=0)), ctrlParams=NS(agc=NS(enable=0)))
d.params = NS(rxChannelA=NS(contents=rx), devParams=None)
d._update = lambda reason, ext=0: sent.append(reason)
d.set_gain(0.0);   assert rx.tunerParams.gain.LNAstate == 8 and sent[-1] == sp.UPD_GR
d.set_gain(100.0); assert rx.tunerParams.gain.LNAstate == 0 and sent[-1] == sp.UPD_GR
rx.tunerParams.gain.gRdB = 33                       # AGC has moved the IF gain; a same-band retune must not undo it
d.set_center(153.2e6); assert rx.tunerParams.gain.gRdB == 33 and sent[-1] == sp.UPD_FRF
d.set_center(1090e6);  assert rx.tunerParams.gain.LNAstate <= 5
print("agc knob ok")
