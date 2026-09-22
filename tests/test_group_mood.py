"""Group behavior using deterministic boxes/emotions; does not measure model accuracy."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from lighting import Lighting, MoodFilter, PALETTE, adalight_frame, load_config, rgb
from group_mood import GroupMood


def result(labels, reverse=False, uncertain=(), mirror=True):
    faces=[dict(x1=i*200+20,y1=30,x2=i*200+150,y2=180,
                emotion=dict(label=label,label_zh=label,uncertain=i in uncertain))
           for i,label in enumerate(labels)]
    return dict(count=len(faces),faces=list(reversed(faces)) if reverse else faces,
                frame=dict(w=1600,h=900),mirror=mirror)


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.now=0
        cfg=load_config(Path(__file__).resolve().parents[1]/'configs/light.json')
        cfg['group_mode']='spatial'
        self.light=Lighting(cfg,clock=lambda:self.now)
        self.light.session('start','group-test')
        self.seq=0

    def put(self,now,labels,**kw):
        self.now=now;self.seq+=1
        payload=result(labels,**kw)
        state=self.light.accept(self.light.ticket('group-test',self.seq),payload)
        return state,payload

    def test_all_agree_tiers_and_no_three_person_cap(self):
        for n,power in [(1,.6),(2,.78),(3,.78),(4,.95),(6,.95)]:
            self.light.session('stop','group-test');self.light.session('start','group-test')
            start=self.now+.1
            self.put(start,['happy']*n)
            state,_=self.put(start+.35,['happy']*n)
            self.assertTrue(state['group']['agreed'])
            self.assertEqual(len(state['group']['people']),n)
            self.assertEqual(state['transition']['to_power'],power)

    def test_mixed_proportions_spatial_pixels_and_mirror(self):
        labels=['happy','happy','sad']
        self.put(0,labels)
        state,_=self.put(.35,labels)
        self.assertEqual(state['current']['label'],'mixed')
        self.assertEqual({c['label']:c['count'] for c in state['group']['composition']},{'happy':2,'sad':1})
        pixels=state['transition']['to_pixels']
        self.assertEqual(pixels[0],rgb(PALETTE['sad']))
        self.assertEqual(pixels[-1],rgb(PALETTE['happy']))
        self.assertTrue(any(p not in [rgb(PALETTE['sad']),rgb(PALETTE['happy'])] for p in pixels))
        self.assertEqual(state['transition']['to_power'],.6)
        state,_=self.put(.7,labels,mirror=False)
        self.assertEqual(state['transition']['to_pixels'][0],rgb(PALETTE['happy']))

    def test_detector_order_and_motion_keep_ids(self):
        _,first=self.put(0,['happy','sad'])
        state,second=self.put(.35,['happy','sad'],reverse=True)
        self.assertEqual([p['track_id'] for p in first['faces']],[p['track_id'] for p in reversed(second['faces'])])
        moving=result(['happy','sad'])
        for face in moving['faces']:
            face['x1']+=20;face['x2']+=20
        self.now=.7
        self.light.accept(self.light.ticket('group-test',3),moving)
        self.assertEqual([p['track_id'] for p in first['faces']],[p['track_id'] for p in moving['faces']])

    def test_pending_prevents_boost_and_uncertain_expires_independently(self):
        self.put(0,['happy','happy'],uncertain=(1,))
        state,_=self.put(.35,['happy','happy'],uncertain=(1,))
        self.assertFalse(state['group']['agreed'])
        self.assertEqual(state['transition']['to_power'],.6)
        self.put(.7,['happy','sad'])
        self.put(1.05,['happy','sad'])
        state,_=self.put(1.4,['happy','sad'],uncertain=(1,))
        self.assertEqual(state['current']['label'],'mixed')
        state,_=self.put(2.31,['happy','sad'],uncertain=(1,))
        self.assertEqual(state['group']['confirmed_count'],1)
        self.assertEqual(state['transition']['to_power'],.6)
        self.assertFalse(state['group']['agreed'])

    def test_leave_grace_return_single_stop_and_epoch(self):
        self.put(0,['happy','sad']);self.put(.35,['happy','sad'])
        state,_=self.put(.7,['happy'])
        self.assertEqual(state['group']['confirmed_count'],2)
        self.now=1.59
        self.assertEqual(self.light.snapshot()['group']['confirmed_count'],2)
        state,_=self.put(1.61,['happy'])
        self.assertEqual(len(state['group']['people']),1)
        self.assertEqual(state['current']['label'],'happy')
        ticket=self.light.ticket('group-test',99)
        self.light.session('stop','group-test')
        state=self.light.accept(ticket,result(['sad']*4))
        self.assertEqual(state['group']['people'],[])
        self.assertEqual(state['mode'],'standby')

    def test_no_faces_and_stall_return_standby(self):
        self.put(0,['happy','sad']);self.put(.35,['happy','sad'])
        self.put(.7,[])
        self.now=1.61
        self.assertEqual(self.light.snapshot()['mode'],'standby')
        self.put(2,['happy','sad']);self.put(2.35,['happy','sad'])
        self.now=4.26
        self.assertEqual(self.light.snapshot()['mode'],'standby')

    def test_pixel_packet_keeps_distinct_colors(self):
        packet=adalight_frame([(255,0,0),(0,0,255)],2)
        self.assertEqual(packet,b'Ada\x00\x01\x54\xff\x00\x00\x00\x00\xff')
        with self.assertRaises(ValueError):adalight_frame([(1,2,3)],2)

    def test_cycle_keeps_composition_and_obeys_owner(self):
        self.assertFalse(self.light.layout('cycle','observer'))
        self.assertTrue(self.light.layout('cycle','group-test'))
        self.put(0,['happy','happy','sad']);state,_=self.put(.35,['happy','happy','sad'])
        key=state['group']['key']
        self.assertEqual(state['output_label'],'happy')
        for t in [.7,1.05,1.4,1.75,2.1,2.45,2.8]:
            state,_=self.put(t,['happy','happy','sad'])
        self.assertEqual(state['output_label'],'sad')
        self.assertEqual(state['group']['key'],key)
        self.assertEqual(state['transition']['to_pixels'],[rgb(PALETTE['sad'])]*30)
        self.assertEqual(state['transition']['to_power'],.6)


if __name__=='__main__':unittest.main(verbosity=2)
