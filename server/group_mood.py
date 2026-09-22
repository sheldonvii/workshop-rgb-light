"""Short-lived geometric tracks and independent emotion confirmation, not identity."""
import copy
import math


def match_cost(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    scale = max(.01, math.hypot(aw, ah), math.hypot(bw, bh))
    distance = math.hypot(ax - bx, ay - by) / scale
    ratio = max(aw * ah, bw * bh) / max(.00001, min(aw * ah, bw * bh))
    return distance if distance <= .8 and ratio <= 3 else math.inf


class GroupMood:
    def __init__(self, filter_factory):
        self.filter_factory = filter_factory
        self.reset()

    def reset(self):
        self.tracks = {}
        self.next_id = 1
        self.last_sample = None
        self.current = None
        self.status = 'waiting'
        self.mirror = True
        self.people = []
        self.composition = []
        self.agreed = False
        self.key = ''

    def accept(self, result, now):
        self.tick(now)
        faces = result.get('faces', [])
        # Older single-face clients can still drive the original contract.
        if not faces and result['count'] == 1:
            faces = [dict(x1=0, y1=0, x2=1, y2=1, emotion=result.get('primary'))]
        w, h = (result.get('frame') or {}).get('w', 1), (result.get('frame') or {}).get('h', 1)
        boxes = [((f['x1'] + f['x2']) / (2*w), (f['y1'] + f['y2']) / (2*h),
                  (f['x2'] - f['x1']) / w, (f['y2'] - f['y1']) / h) for f in faces]
        candidates = sorted((match_cost(t['position'], box), tid, i)
                            for tid, t in self.tracks.items() for i, box in enumerate(boxes))
        used, assignments = set(), {}
        for cost, tid, i in candidates:
            if math.isfinite(cost) and tid not in used and i not in assignments:
                assignments[i] = tid
                used.add(tid)
        for i, face in enumerate(faces):
            tid = assignments.get(i)
            if tid is None:
                tid = self.next_id
                self.next_id += 1
                self.tracks[tid] = {'filter': self.filter_factory()}
            track = self.tracks[tid]
            track.update(position=boxes[i], box=[face[k] for k in ('x1','y1','x2','y2')],
                         raw=copy.deepcopy(face.get('emotion')), seen=now, visible=True)
            track['filter'].accept(face.get('emotion'), 1, now)
            face['track_id'] = tid
            used.add(tid)
        for tid, track in self.tracks.items():
            if tid not in used:
                track['visible'] = False
                track['filter'].accept(None, 0, now)
        self.last_sample = now
        self.mirror = result.get('mirror', True)
        self.tick(now)

    def tick(self, now):
        for tid, track in list(self.tracks.items()):
            track['filter'].tick(now)
            missing_at = track['filter'].invalid_at
            if not track['visible'] and missing_at is not None and now - missing_at >= .9:
                del self.tracks[tid]
        self.people = [dict(id=tid, box=t['box'], position=t['position'][0], visible=t['visible'],
                            current=copy.deepcopy(t['filter'].current), raw=t['raw'], status=t['filter'].status)
                       for tid, t in self.tracks.items()]
        self.people.sort(key=lambda p: p['position'], reverse=self.mirror)
        active = [p for p in self.people if p['current']]
        counts = {}
        for p in active:
            label = p['current']['label']
            if label not in counts:
                counts[label] = dict(label=label, label_zh=p['current'].get('label_zh', label), count=0)
            counts[label]['count'] += 1
        self.composition = list(counts.values())
        self.agreed = bool(active) and len(active) == len(self.people) and len(counts) == 1 and all(
            p['visible'] and p['status'] == 'stable' for p in self.people)
        self.status = 'stable' if active and all(p['visible'] and p['status'] == 'stable' for p in self.people) else 'holding' if active else 'waiting'
        self.current = None
        if active:
            if len(self.people) == 1:
                self.current = copy.deepcopy(active[0]['current'])
            else:
                label = next(iter(counts)) if len(counts) == 1 else 'mixed'
                zh = next(iter(counts.values()))['label_zh'] if len(counts) == 1 else '多种情绪'
                self.current = dict(label=label, label_zh=zh, score_type='group', uncertain=False)
        self.key = '|'.join(f'{label}:{counts[label]["count"]}' for label in sorted(counts))

    def snapshot(self):
        return dict(people=self.people, composition=self.composition, agreed=self.agreed,
                    key=self.key, visible_count=sum(p['visible'] for p in self.people),
                    confirmed_count=sum(c['count'] for c in self.composition), mirror=self.mirror)


def zone_pixels(people, palette, count, standby):
    colors = [palette[p['current']['label']] for p in people if p['current']]
    if not colors:
        return [standby[:] for _ in range(count)]
    pixels = [colors[min(len(colors)-1, i * len(colors) // count)][:] for i in range(count)]
    # Keep recognizable solid zones, blend only around adjacent boundaries.
    width = min(2., count / len(colors) / 4)
    for j in range(1, len(colors)):
        edge = j * count / len(colors)
        for i in range(max(0, math.floor(edge-width)), min(count, math.ceil(edge+width))):
            weight = max(0, min(1, (i + .5 - edge + width) / (2*width)))
            pixels[i] = [round(a+(b-a)*weight) for a,b in zip(colors[j-1], colors[j])]
    return pixels
