"""以唯讀地圖規劃保守路線；避開物件與非目的地事件格。"""
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import struct

from .session import ProtocolError


@dataclass(frozen=True)
class MapGrid:
    width: int
    height: int
    layers: tuple = field(repr=False)

    @classmethod
    def load(cls, path: Path):
        data = path.read_bytes()
        if len(data) < 8:
            raise ProtocolError("地圖標頭不完整，已停止")
        w, h = struct.unpack_from("<II", data)
        if not 0 < w <= 8192 or not 0 < h <= 8192 or w * h > 1000000 or len(data) != 8 + w * h * 6:
            raise ProtocolError("地圖尺寸或長度不符，已停止")
        n = w * h
        return cls(w, h, tuple(struct.unpack_from(f"<{n}H", data, 8 + k * n * 2) for k in range(3)))

    def walkable(self, point, *, endpoints=()):
        x, y = point
        if not 0 <= x < self.width or not 0 <= y < self.height:
            return False
        i = y * self.width + x
        floor, objects, flags = (layer[i] for layer in self.layers)
        return bool(floor and objects == 0 and flags & 0xC000 == 0xC000
                    and (flags & 0x3FFF == 0 or point in endpoints))

    def path(self, start, goal, *, maximum_steps=256):
        if not 1 <= maximum_steps <= 4096:
            raise ValueError("路線步數上限錯誤")
        if not self.walkable(start, endpoints=(start, goal)) or not self.walkable(goal, endpoints=(start, goal)):
            raise ProtocolError("路線起點或終點不可通行，已停止")
        queue = deque([start])
        parents = {start: None}
        directions = ((0, -1, b"a"), (1, 0, b"c"), (0, 1, b"e"), (-1, 0, b"g"))
        while queue:
            point = queue.popleft()
            if point == goal:
                break
            for dx, dy, code in directions:
                next_point = point[0] + dx, point[1] + dy
                if next_point not in parents and self.walkable(next_point, endpoints=(start, goal)):
                    parents[next_point] = point, code
                    queue.append(next_point)
        if goal not in parents:
            raise ProtocolError("找不到可確認的通行路線，已停止")
        steps = []
        point = goal
        while point != start:
            previous, code = parents[point]
            steps.append((previous[0], previous[1], code))
            point = previous
            if len(steps) > maximum_steps:
                raise ProtocolError("路線超過步數上限，已停止")
        steps.reverse()
        return tuple((steps[i][0], steps[i][1], b"".join(step[2] for step in steps[i:i + 2]))
                     for i in range(0, len(steps), 2))
