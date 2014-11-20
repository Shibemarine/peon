import threading
import time
import logging
from player import Player
import types
from fastmc.proto import Position


log = logging.getLogger(__name__)


class Robot(Player):

    def __init__(self, proto, send_queue, recv_condition, world):
        super(Robot, self).__init__(proto, send_queue, recv_condition, world)
        self._movement_lock = threading.Lock()
        self._action_lock = threading.Lock()
        self._auto_actions = {
            'defend': threading.Event(),
            'eat': threading.Event(),
            'hunt': threading.Event(),
            'gather': threading.Event(),
        }
        self.auto_defend_mob_types = types.HOSTILE_MOBS
        self.auto_gather_items = set([])
        self._auto_eat = threading.Event()
        self._auto_eat_level = 18
        self.auto_hunt_settings = {}
        self._threads = {}
        self._thread_funcs = {
            'falling': self._do_falling,
            'auto_defend': self._do_auto_defend,
            'auto_eat': self._do_auto_eat,
            'auto_hunt': self._do_auto_hunt,
            'auto_gather': self._do_auto_gather,
        }
        self._active_threads = set(self._thread_funcs.keys())
        self.start_threads()

    def __repr__(self):
        return 'Bot(xyz={}, health={}, food={}, xp={}, auto_actions={})'.format(
            self.get_position(floor=True),
            self.health,
            self.food,
            self.xp_level,
            self.auto_actions,
        )

    @property
    def auto_actions(self):
        return [n for n, e in self._auto_actions.iteritems() if e.is_set()]

    def start_threads(self):
        for name, func in self._thread_funcs.iteritems():
            thread = threading.Thread(target=func, name=name)
            thread.daemon = True
            thread.start()
            self._threads[name] = thread

    def _do_falling(self):
        while True:
            if self._is_moving.is_set():
                continue
            pos = self.position
            if None in pos:
                continue
            x, y, z = pos
            standing = self.world.is_solid_block(x, y - 1, z)
            if standing is None or standing:
                continue
            next_pos = self.world.get_next_highest_solid_block(x, y, z)
            if next_pos is None:
                continue
            self.on_ground = False
            x, y, z = next_pos
            self.on_ground = self.move_to(x, y + 1, z, speed=13)
            time.sleep(0.1)

    def _do_auto_defend(self):
        auto_defend = self._auto_actions.get('defend')
        while True:
            auto_defend.wait()
            eids_in_range = [e.eid for e in self.iter_entities_in_range(
                self.auto_defend_mob_types)]
            if not eids_in_range:
                time.sleep(0.1)
                continue
            with self._action_lock:
                self.equip_any_item_from_list([
                    'Diamond Sword',
                    'Golden Sword',
                    'Iron Sword',
                    'Stone Sword',
                    'Wooden Sword',
                ])
                for eid in eids_in_range:
                    self._send(self.proto.PlayServerboundUseEntity.id,
                               target=eid,
                               type=1
                               )
            time.sleep(0.1)

    def _do_auto_eat(self):
        auto_eat = self._auto_actions.get('eat')
        self._wait_for(lambda: None not in (self.inventory, self.food))
        while True:
            auto_eat.wait()
            if self.food < self._auto_eat_level:
                if not self.eat(self._auto_eat_level):
                    log.warn('Hungry, but no food!')
            time.sleep(10)

    def _do_auto_hunt(self):
        auto_hunt = self._auto_actions.get('hunt')
        self._wait_for(
            lambda: None not in (self.inventory, self.food, self.health))
        while True:
            auto_hunt.wait()
            self.hunt(**self.auto_hunt_settings)
            time.sleep(1)

    def _do_auto_gather(self):
        auto_gather = self._auto_actions.get('gather')
        self._wait_for(
            lambda: None not in (self.inventory, self.food, self.health))
        while True:
            auto_gather.wait()
            self.gather(self.auto_gather_items)
            time.sleep(1)

    def enable_auto_action(self, name):
        auto_action = self._auto_actions.get(name)
        if auto_action is None:
            return False
        auto_action.set()
        return True

    def disable_auto_action(self, name):
        auto_action = self._auto_actions.get(name)
        if auto_action is None:
            return False
        auto_action.clear()
        return True

    def set_auto_defend_mob_types(self, mob_types):
        self.auto_defend_mob_types = mob_types

    def eat(self, target=20):
        if self.food >= target:
            return True
        with self._action_lock:
            if not self.equip_any_item_from_list(types.FOOD):
                return False
            log.info('Eating: %s', self.held_item.name)
            while self.held_item is not None and self.food < target:
                count = self.held_item.count
                self._send(self.proto.PlayServerboundBlockPlacement.id,
                           location=Position(-1, 255, -1),
                           direction=-1,
                           held_item=self.held_item,
                           cursor_x=-1,
                           cursor_y=-1,
                           cursor_z=-1)
                self._wait_for(
                    lambda: (
                        self.held_item is None or
                        self.held_item.count < count
                    )
                )
            self._send(self.proto.PlayServerboundPlayerDigging.id,
                       status=5,
                       location=Position(0, 0, 0),
                       face=127)
        return self.food >= target

    def hunt(self, home=None, mob_types=None, space=3, speed=10, _range=50):
        if not self.health or self.health <= 10:
            log.warn('health unknown or too low: %s', self.health)
            return False
        self.enable_auto_action('defend')
        if mob_types is None:
            mob_types = types.HOSTILE_MOBS
        with self._movement_lock:
            home = self.get_position(floor=True) if home is None else home
            if not self.navigate_to(*home, timeout=30):
                log.warn('failed nav to home')
                return False
            x0, y0, z0 = home
            for entity in self.iter_entities_in_range(mob_types, reach=_range):
                log.info("hunting entity: %s", str(entity))
                x, y, z = entity.get_position(floor=True)
                path = self.world.find_path(x0, y0, z0, x, y, z, space=space,
                                            timeout=10)
                if path:
                    break
            else:
                return False
            self.follow_path(path)
            self.attack_entity(entity)
            self.navigate_to(*path[-1])
            path.reverse()
            path.append(home)
            return self.follow_path(path)

    def gather(self, items, _range=50):
        with self._movement_lock:
            x0, y0, z0 = self.get_position(floor=True)
            for _object in self.iter_objects_in_range(items=items, reach=_range):
                log.info("gathering object: %s", str(_object))
                x, y, z = _object.get_position(floor=True)
                path = self.world.find_path(x0, y0, z0, x, y, z, space=1,
                                            timeout=30)
                if path:
                    break
            else:
                return False
            self.follow_path(path)
            path.reverse()
            path.append((x0, y0, z0))
            return self.follow_path(path)

    def attack_entity(self, entity, space=3, timeout=6):
        on_kill_list = entity._type in self.auto_defend_mob_types
        if not on_kill_list:
            self.auto_defend_mob_types.add(entity._type)
        start = time.time()
        while self.health > 10 and entity.eid in self.world.entities:
            x, y, z = entity.get_position(floor=True)
            if not self.navigate_to(x, y, z, space=space, timeout=2):
                break
            elif time.time() - start > timeout:
                break
            time.sleep(0.1)
        if not on_kill_list:
            self.auto_defend_mob_types.remove(entity._type)