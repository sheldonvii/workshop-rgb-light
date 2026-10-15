"""用短期几何轨迹关联逐脸情绪，不做身份识别。"""

import copy
import math


def match_cost(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    scale = max(0.01, math.hypot(aw, ah), math.hypot(bw, bh))
    distance = math.hypot(ax - bx, ay - by) / scale
    ratio = max(aw * ah, bw * bh) / max(0.00001, min(aw * ah, bw * bh))
    return distance if distance <= 0.8 and ratio <= 3 else math.inf


def normalized_boxes(faces, frame):
    width, height = frame["w"], frame["h"]
    return [
        (
            (face["x1"] + face["x2"]) / (2 * width),
            (face["y1"] + face["y2"]) / (2 * height),
            (face["x2"] - face["x1"]) / width,
            (face["y2"] - face["y1"]) / height,
        )
        for face in faces
    ]


class GroupMood:
    def __init__(self, filter_factory, expire_grace_seconds):
        self.filter_factory = filter_factory
        self.expire_grace_seconds = expire_grace_seconds
        self.reset()

    def reset(self):
        self.tracks = {}
        self.next_id = 1
        self.last_sample = None
        self.current = None
        self.status = "waiting"
        self.mirror = True
        self.people = []
        self.composition = []
        self.agreed = False
        self.key = ""

    def accept(self, result, now):
        self.tick(now)
        faces = result["faces"]
        boxes = normalized_boxes(faces, result["frame"])
        visible_ids = self._update_visible_faces(faces, boxes, now)
        for track_id, track in self.tracks.items():
            if track_id not in visible_ids:
                track["visible"] = False
                track["filter"].accept(None, 0, now)
        self.last_sample = now
        self.mirror = result.get("mirror", True)
        self.tick(now)

    def _match_faces(self, boxes):
        # 贪心按中心距离和面积比关联，只维持短时编号，不保证遮挡后的身份。
        candidates = sorted(
            (match_cost(track["position"], box), track_id, index)
            for track_id, track in self.tracks.items()
            for index, box in enumerate(boxes)
        )
        assigned_tracks, assignments = set(), {}
        for cost, track_id, index in candidates:
            if (
                math.isfinite(cost)
                and track_id not in assigned_tracks
                and index not in assignments
            ):
                assignments[index] = track_id
                assigned_tracks.add(track_id)
        return assignments

    def _update_visible_faces(self, faces, boxes, now):
        assignments = self._match_faces(boxes)
        visible_ids = set()
        for index, face in enumerate(faces):
            track_id = assignments.get(index)
            if track_id is None:
                track_id = self.next_id
                self.next_id += 1
                self.tracks[track_id] = {"filter": self.filter_factory()}
            track = self.tracks[track_id]
            track.update(
                position=boxes[index],
                box=[face[key] for key in ("x1", "y1", "x2", "y2")],
                raw=copy.deepcopy(face["emotion"]),
                visible=True,
            )
            track["filter"].accept(face["emotion"], 1, now)
            face["track_id"] = track_id
            visible_ids.add(track_id)
        return visible_ids

    def tick(self, now):
        for tid, track in list(self.tracks.items()):
            track["filter"].tick(now)
            missing_at = track["filter"].invalid_at
            if (
                not track["visible"]
                and missing_at is not None
                and now - missing_at >= self.expire_grace_seconds
            ):
                del self.tracks[tid]
        self.people = [
            dict(
                id=tid,
                box=t["box"],
                position=t["position"][0],
                visible=t["visible"],
                current=copy.deepcopy(t["filter"].current),
                raw=t["raw"],
                status=t["filter"].status,
            )
            for tid, t in self.tracks.items()
        ]
        self.people.sort(key=lambda p: p["position"], reverse=self.mirror)
        self._compose()

    def _compose(self):
        active = [p for p in self.people if p["current"]]
        counts = {}
        for p in active:
            label = p["current"]["label"]
            if label not in counts:
                counts[label] = dict(
                    label=label, label_zh=p["current"].get("label_zh", label), count=0
                )
            counts[label]["count"] += 1
        self.composition = list(counts.values())
        self.agreed = (
            bool(active)
            and len(active) == len(self.people)
            and len(counts) == 1
            and all(p["visible"] and p["status"] == "stable" for p in self.people)
        )
        if not active:
            self.status = "waiting"
        elif all(
            person["visible"] and person["status"] == "stable" for person in self.people
        ):
            self.status = "stable"
        else:
            self.status = "holding"
        self.current = None
        if active:
            if len(self.people) == 1:
                self.current = copy.deepcopy(active[0]["current"])
            else:
                label = next(iter(counts)) if len(counts) == 1 else "mixed"
                zh = (
                    next(iter(counts.values()))["label_zh"]
                    if len(counts) == 1
                    else "多种情绪"
                )
                self.current = dict(
                    label=label, label_zh=zh, score_type="group", uncertain=False
                )
        self.key = "|".join(
            f"{label}:{counts[label]['count']}" for label in sorted(counts)
        )

    def snapshot(self):
        return dict(
            people=self.people,
            composition=self.composition,
            agreed=self.agreed,
            key=self.key,
            visible_count=sum(p["visible"] for p in self.people),
            confirmed_count=sum(c["count"] for c in self.composition),
            mirror=self.mirror,
        )


def zone_pixels(people, palette, count, standby):
    colors = [palette[p["current"]["label"]] for p in people if p["current"]]
    if not colors:
        return [standby[:] for _ in range(count)]
    pixels = [
        colors[min(len(colors) - 1, i * len(colors) // count)][:] for i in range(count)
    ]
    # 只混合相邻分区的边界，保留可辨认的纯色区；灯珠顺序与画面镜像一致。
    width = min(2.0, count / len(colors) / 4)
    for j in range(1, len(colors)):
        edge = j * count / len(colors)
        for i in range(
            max(0, math.floor(edge - width)), min(count, math.ceil(edge + width))
        ):
            weight = max(0, min(1, (i + 0.5 - edge + width) / (2 * width)))
            pixels[i] = [
                round(a + (b - a) * weight) for a, b in zip(colors[j - 1], colors[j])
            ]
    return pixels
