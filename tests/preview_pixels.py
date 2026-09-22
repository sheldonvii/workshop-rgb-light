"""Manual physical test. Stop the demo first; always restore its standby color."""
import argparse
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from lighting import USBStrip, load_config, rgb


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true',help='send visible test patterns to the connected strip')
    parser.add_argument('--seconds',type=float,default=15,help='hold each comparison pattern; default 15 seconds')
    args=parser.parse_args()
    if not args.run:
        parser.error('explicit --run required')
    if not 1 <= args.seconds <= 30:parser.error('--seconds must be between 1 and 30')
    cfg=load_config(Path(__file__).resolve().parents[1]/'configs/light.json')
    strip=USBStrip(cfg);n=cfg['led_count']
    def hold(pixels,seconds):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            strip.send(pixels);time.sleep(.1)
    strip.connect()
    try:
        print('1: red first half / blue second half',flush=True)
        hold([(64,0,0) if i<n//2 else (0,0,64) for i in range(n)],args.seconds)
        print('2: blue first half / red second half',flush=True)
        hold([(0,0,64) if i<n//2 else (64,0,0) for i in range(n)],args.seconds)
        print('3: red-to-blue spatial gradient',flush=True)
        hold([(round(64*(1-i/max(1,n-1))),0,round(64*i/max(1,n-1))) for i in range(n)],args.seconds)
        print('4: green section moving across six zones',flush=True)
        for zone in range(6):
            print('green zone',zone+1,flush=True)
            hold([(0,64,0) if i*6//n==zone else (0,0,0) for i in range(n)],args.seconds/3)
        print('5: whole-strip red-to-blue temporal fade',flush=True)
        steps=round(args.seconds*10)
        for step in range(steps+1):
            strip.send((round(64*(1-step/steps)),0,round(64*step/steps)));time.sleep(.1)
    finally:
        strip.send(tuple(round(c*cfg['standby_brightness']) for c in rgb(cfg['standby_color'])))
        strip.close()
        print('standby restored',flush=True)


if __name__=='__main__':main()
