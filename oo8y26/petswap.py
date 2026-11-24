"""騎寵、戰寵練滿（140 級）就換一隻還沒滿的上來（使用者 2026-11-24 指定）。

規則：
- 戰寵滿級：從個人寵物倉庫找「名稱在練功計畫戰寵名單上、未滿 140 級」的，挑等級最高的那一隻換上；倉庫沒有就記一行、不換。
- 騎寵滿級：先從個人寵物倉庫找可以騎的——名稱和現在這隻相同（造型一定騎得上），或在這隻角色的騎寵名單上；
  要未滿 140 級，而且等級不超過人物等級＋5（超過騎不上）。倉庫沒有，就到商城石幣分頁買一顆「自选老虎蛋」
  （10000 石幣）開出這隻角色騎得上的那一種老虎（現在騎的就是老虎就開同一種，否則用計畫檔 role_mounts 指定的）。
- 滿級的那一隻存進個人寵物倉庫（不丟）。倉庫滿了、寵物欄也滿了就不換，記一行。

換的動作只在角色單獨登入、不在戰鬥中時做（分級練功的整備步驟）。這裡只把寵物換到身上，
騎上去和設成出戰由組隊後的 ensure_pets 照名稱處理（同名的滿級那一隻已經存進倉庫，身上只剩一隻）。
出錯就停：伺服器不受理直接丟 ProtocolError，不重試。只用石幣。
"""
from __future__ import annotations

import time

from . import mall
from .session import ProtocolError, Session
from .storage import PersonalPetStorage

MAX_PET_LEVEL = 140
RIDE_LEVEL_GAP = 5                    # 騎寵等級最多比人物高幾級
EGG = "自选老虎蛋"
EGG_PRICE = 10000
TIGERS = ("贝鲁卡", "贝鲁伊卡", "格鲁西斯", "金格萨贝鲁")       # 自選蛋視窗的選項順序（1～4）


def _name(pet) -> str:
    return pet.name.decode("gbk", "replace")


def pick_fighter(stored: list, names: tuple):
    """倉庫清單 [(名稱, 等級, 編號)…] 裡，名單上、未滿級、等級最高的那一隻；沒有回 None。"""
    usable = [entry for entry in stored if entry[0] in names and entry[1] < MAX_PET_LEVEL]
    return max(usable, key=lambda entry: entry[1]) if usable else None


def pick_mount(stored: list, names: tuple, character_level: int):
    """倉庫清單裡可以騎的：名單上（越前面越優先）、未滿級、等級不超過人物＋5；同名取等級最高的。沒有回 None。"""
    usable = [entry for entry in stored
              if entry[0] in names and entry[1] < MAX_PET_LEVEL and entry[1] <= character_level + RIDE_LEVEL_GAP]
    return min(usable, key=lambda entry: (names.index(entry[0]), -entry[1])) if usable else None


def tiger_for(current: str, listed: tuple):
    """買蛋時要開哪一種老虎：現在騎的就是老虎就開同一種，否則用這隻角色名單上第一種老虎；都沒有回 None。"""
    return current if current in TIGERS else next((name for name in listed if name in TIGERS), None)


def _exchange(session: Session, store: PersonalPetStorage, old_index: int, bring, say, label: str) -> bool:
    """把寵物欄 old_index 那一隻存進倉庫、再用 bring() 把新的弄到身上；倉庫和寵物欄都滿就不換。"""
    world = session.world
    old = f"{_name(world.pets[old_index])}（{world.pets[old_index].level} 級）"
    if store.count < store.capacity:
        store.deposit(old_index)
        time.sleep(1.5)
        bring()
    elif len(world.pets) < 5:
        bring()
        time.sleep(1.5)
        store.deposit(old_index)
    else:
        say(f"{label}{old}滿級了，但個人寵物倉庫和寵物欄都滿，換不了")
        return False
    return True


def swap_maxed(session: Session, outfitter, slot: int, *, mount_names: tuple, fighter_names: tuple,
               character_level: int, say=print) -> list:
    """檢查這隻角色的騎寵、戰寵，滿級就換；回傳做了的事（文字）。outfitter 是 newbie.Outfitter（開蛋要用）。"""
    world = session.world
    if session.battle.active:
        return []
    done = []
    fighter, mount = world.battle_pet_index, world.mount_pet_index
    maxed = lambda index: index in world.pets and (world.pets[index].level or 0) >= MAX_PET_LEVEL
    if not (maxed(fighter) or maxed(mount)):
        return done
    store = PersonalPetStorage(session).open()
    if maxed(fighter):
        found = pick_fighter(store.listing(), fighter_names)
        if found is None:
            say(f"戰寵{_name(world.pets[fighter])}滿級了，個人寵物倉庫沒有未滿 {MAX_PET_LEVEL} 級的可換")
        else:
            session._set_battle_pet(slot, -1)             # 出戰中的不能存倉庫：先收起來
            time.sleep(1.0)
            old = _name(world.pets[fighter])
            if _exchange(session, store, fighter, lambda: store.withdraw(found[2]), say, "戰寵"):
                done.append(f"戰寵 {old}（滿級）存倉庫，換上倉庫的 {found[0]}（{found[1]} 級）")
            time.sleep(1.5)
    if maxed(mount):
        current = _name(world.pets[mount])
        names = tuple(dict.fromkeys((current,) + tuple(mount_names)))
        found = pick_mount(store.listing(), names, character_level)
        tiger = tiger_for(current, tuple(mount_names))
        if found is None and tiger is None:
            say(f"騎寵{current}滿級了，個人寵物倉庫沒有可以騎的，也不知道這隻角色騎得上哪一種老虎，沒有換")
            return done
        # 騎著的不能存倉庫：對正在騎的那一隻再送一次騎乘指令就是下來（2026-11-20 實測，見 Session._ride）
        session._send_command(94, (f"R|P|{mount}".encode(),), time.monotonic() + session.timeout)
        end = time.monotonic() + 6.0
        while time.monotonic() < end and world.mount_pet_index == mount:
            session.poll_event(0.25)
        if world.mount_pet_index == mount:
            raise ProtocolError(f"騎寵{current}滿級要換，但下不了騎，已停止")
        time.sleep(1.0)

        def bring():
            if found is not None:
                store.withdraw(found[2])
                return
            if not world.has_item(EGG):
                mall.buy_with_stone(session, [EGG], say=lambda message: None, maximum_price=EGG_PRICE)
                outfitter.items = {key: value[0] for key, value in (world.items or {}).items() if value and value[0]}
            if not outfitter.choose(EGG, TIGERS.index(tiger) + 1, tiger):
                raise ProtocolError(f"開{EGG}失敗，已停止")

        if _exchange(session, store, mount, bring, say, "騎寵"):
            done.append(f"騎寵 {current}（滿級）存倉庫，" + (f"換上倉庫的 {found[0]}（{found[1]} 級）" if found is not None
                                                        else f"買{EGG}（{EGG_PRICE} 石幣）開出 {tiger}"))
    for line in done:
        say(line)
    return done
