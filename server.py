"""Сервер «Арены»: мультиплеер для Ракетки и Хоккея и раздача index.html.

Тако — виртуальная валюта. Баланс хранится на сервере, купить или вывести тако нельзя.
С каждого выигрыша удерживается комиссия FEE (20%): эти тако сгорают и никому не начисляются.
"""
import asyncio
import hashlib
import hmac
import json
import math
import os
import random
import re
import sqlite3
import time
import urllib.parse

FEE = 0.20                 # комиссия с выигрыша (с прибыли), сгорает
MIN_BET, MAX_BET = 10, 1_000_000
START_BALANCE = 1000
RESET_BELOW = 100          # «начать заново» доступно, только если тако почти не осталось

R_WAIT, R_CRASH_PAUSE, R_GROWTH = 5.0, 2.6, 0.12
H_LOBBY, H_PLAY, H_RESULT = 15.0, 5.6, 3.8

NAMES = ['Артём', 'Дима', 'Стас', 'Лена', 'Макс', 'Ника', 'Игорь', 'Соня', 'Влад', 'Кира', 'Ян', 'Полина',
         'Гоша', 'Аня', 'Тимур', 'Ева']
ACH_KEYS = ('first', 'x5', 'goal', 'team', 'streak3', 'rich')


def now():
    return time.monotonic()


def fee_on(gross, stake):
    """Комиссия берётся с прибыли (выплата минус собственная ставка)."""
    return max(0, math.floor((gross - stake) * FEE))


def crash_point():
    return min(200.0, max(1.0, math.floor(96 / (1 - random.random())) / 100))


def clean_name(s):
    s = re.sub(r'[\x00-\x1f\x7f<>]', '', str(s or '')).strip()
    return s[:24] or 'Игрок'


def day_key(ts=None):
    return time.strftime('%Y-%m-%d', time.gmtime(ts))


# ---------------------------------------------------------------- пользователи

class User:
    def __init__(self, uid, name):
        self.uid, self.name = uid, name
        self.balance, self.games, self.wins, self.best, self.total_won = START_BALANCE, 0, 0, 0, 0
        self.since, self.last_bonus, self.streak, self.ach = time.time(), '', 0, {}
        self.conns = set()

    def bonus(self):
        nxt = self.streak + 1 if self.last_bonus == day_key(time.time() - 86400) else 1
        return {'claimed': self.last_bonus == day_key(), 'amount': 300 + 100 * min(nxt - 1, 5), 'next': nxt}

    def public(self):
        return {'t': 'me', 'uid': self.uid, 'name': self.name, 'balance': self.balance, 'games': self.games,
                'wins': self.wins, 'best': self.best, 'totalWon': self.total_won, 'since': int(self.since * 1000),
                'ach': self.ach, 'bonus': self.bonus()}


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('create table if not exists users(uid text primary key, name text, balance integer, games integer,'
                        ' wins integer, best integer, total_won integer, since real, last_bonus text, streak integer, ach text)')
        self.db.commit()
        self.cache = {}

    def get(self, uid, name):
        u = self.cache.get(uid)
        if u:
            u.name = name
            return u
        u = User(uid, name)
        row = self.db.execute('select balance,games,wins,best,total_won,since,last_bonus,streak,ach from users where uid=?', (uid,)).fetchone()
        if row:
            (u.balance, u.games, u.wins, u.best, u.total_won, u.since, u.last_bonus, u.streak, ach) = row
            u.ach = json.loads(ach or '{}')
        self.cache[uid] = u
        self.save(u)
        return u

    def save(self, u):
        self.db.execute('insert or replace into users values(?,?,?,?,?,?,?,?,?,?,?)',
                        (u.uid, u.name, u.balance, u.games, u.wins, u.best, u.total_won, u.since, u.last_bonus, u.streak, json.dumps(u.ach)))
        self.db.commit()

    def top(self, n=10):
        return self.db.execute('select uid,name,total_won from users order by total_won desc, since asc limit ?', (n,)).fetchall()

    def rank(self, u):
        return self.db.execute('select count(*) from users where total_won > ?', (u.total_won,)).fetchone()[0] + 1


def unlock(u, key):
    if key in u.ach:
        return False
    u.ach[key] = 1
    return True


def verify_init(init_data, token, max_age=86400):
    """Проверка подписи Telegram WebApp initData. Возвращает словарь пользователя или None."""
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop('hash', None)
        if not got:
            return None
        check = '\n'.join(f'{k}={v}' for k, v in sorted(pairs.items()))
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get('auth_date', '0')) > max_age:
            return None
        return json.loads(pairs['user'])
    except Exception:
        return None


# ---------------------------------------------------------------- соединения

class Conn:
    def __init__(self, ws):
        self.ws, self.user, self.room = ws, None, None
        self.tokens, self.stamp = 20.0, now()

    async def send(self, obj):
        try:
            await self.ws.send_str(json.dumps(obj, ensure_ascii=False, separators=(',', ':')))
        except Exception:
            pass

    def allow(self):
        t = now()
        self.tokens = min(20.0, self.tokens + (t - self.stamp) * 10)
        self.stamp = t
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


def parse_amount(msg, u):
    try:
        a = int(float(msg.get('amount')))
    except (TypeError, ValueError, OverflowError):
        return None
    return a if MIN_BET <= a <= min(MAX_BET, u.balance) else None


class Room:
    def __init__(self, hub):
        self.hub = hub
        self.conns = set()

    async def broadcast(self, msg):
        if self.conns:
            await asyncio.gather(*(c.send(msg) for c in list(self.conns)), return_exceptions=True)

    async def join(self, conn):
        self.conns.add(conn)
        conn.room = self
        await conn.send(self.state_msg())

    async def leave(self, conn):
        self.conns.discard(conn)
        if conn.room is self:
            conn.room = None
        if conn.user:
            await self.on_leave(conn.user)

    async def on_leave(self, user):
        pass


# ---------------------------------------------------------------- Ракетка

def pub_r(p):
    return {'uid': p['uid'], 'name': p['name'], 'stake': p['stake'], 'state': p['state'], 'at': p['at'],
            'payout': p['payout'], 'fee': p['fee'], 'bot': p['bot']}


def new_p(uid, name, stake, state, user=None, auto=None, bot=False, target=0.0):
    return {'uid': uid, 'name': name, 'stake': stake, 'state': state, 'at': 0, 'payout': 0, 'fee': 0,
            'auto': auto, 'bot': bot, 'target': target, 'u': user}


class RocketRoom(Room):
    def __init__(self, hub):
        super().__init__(hub)
        self.skin = random.randrange(10)
        self.hist = [crash_point() for _ in range(14)]
        self.players, self.nextq, self.bots = {}, {}, []
        self.phase, self.deadline, self.t0, self.cp, self.crash_at, self.last_tick = 'wait', 0, 0, 1.0, 0, 0
        self.new_round()

    def new_round(self):
        self.phase, self.deadline = 'wait', now() + R_WAIT
        self.skin = (self.skin + random.randint(1, 9)) % 10
        self.players = {}
        for uid, p in self.nextq.items():
            p['state'] = 'active'
            self.players[uid] = p
        self.nextq = {}
        names = random.sample(NAMES, 6)
        self.bots = []
        for i, n in enumerate(names):
            r = random.random()
            target = random.uniform(1.1, 2.5) if r < .6 else random.uniform(2.5, 6) if r < .9 else random.uniform(6, 20)
            self.bots.append(new_p(f'bot{i}', n, random.randint(1, 30) * 10, 'active', bot=True, target=target))

    def cur_mult(self):
        return math.exp(R_GROWTH * (now() - self.t0))

    def state_msg(self):
        t = now()
        m = {'t': 'r_state', 'phase': self.phase, 'skin': self.skin, 'hist': [round(x, 2) for x in self.hist],
             'players': [pub_r(p) for p in list(self.players.values()) + self.bots],
             'next': [{'uid': p['uid'], 'stake': p['stake']} for p in self.nextq.values()], 'n': len(self.conns)}
        if self.phase == 'wait':
            m['in'] = int(max(0, self.deadline - t) * 1000)
        elif self.phase == 'fly':
            m['el'] = int((t - self.t0) * 1000)
        else:
            m['el'] = int((t - self.crash_at) * 1000)
            m['cp'] = round(self.cp, 2)
        return m

    def settle_cash(self, p, mult):
        gross = math.floor(p['stake'] * mult)
        fee = fee_on(gross, p['stake'])
        net = gross - fee
        p.update(state='cashed', at=round(mult, 2), payout=net, fee=fee)
        u = p['u']
        new = []
        if u:
            u.balance += net
            u.games += 1
            u.wins += 1
            u.best = max(u.best, net)
            u.total_won += max(0, net - p['stake'])
            for k, ok in (('first', True), ('x5', mult >= 5), ('rich', u.balance >= 5000)):
                if ok and unlock(u, k):
                    new.append(k)
            self.hub.store.save(u)
        return u, new

    async def act(self, conn, t, msg):
        u = conn.user
        if t == 'bet':
            amt = parse_amount(msg, u)
            if amt is None:
                return await conn.send({'t': 'msg', 'text': 'Не хватает тако'})
            try:
                auto = float(msg.get('auto')) if msg.get('auto') else None
            except (TypeError, ValueError):
                auto = None
            auto = min(auto, 200.0) if auto and auto >= 1.1 else None
            cur = self.players.get(u.uid)
            if self.phase == 'wait':
                if cur:
                    return
                self.players[u.uid] = new_p(u.uid, u.name, amt, 'active', user=u, auto=auto)
            else:
                if u.uid in self.nextq or (cur and cur['state'] == 'active'):
                    return
                self.nextq[u.uid] = new_p(u.uid, u.name, amt, 'queued', user=u, auto=auto)
            u.balance -= amt
            self.hub.store.save(u)
            await self.broadcast(self.state_msg())
            await self.hub.push_me(u)
        elif t == 'cancel':
            cur = self.players.get(u.uid)
            if self.phase == 'wait' and cur and cur['state'] == 'active':
                del self.players[u.uid]
                u.balance += cur['stake']
            elif u.uid in self.nextq:
                u.balance += self.nextq.pop(u.uid)['stake']
            else:
                return
            self.hub.store.save(u)
            await self.broadcast(self.state_msg())
            await self.hub.push_me(u)
        elif t == 'cash':
            p = self.players.get(u.uid)
            if self.phase != 'fly' or not p or p['state'] != 'active':
                return
            m = self.cur_mult()
            if m >= self.cp:
                return
            _, new = self.settle_cash(p, m)
            await self.broadcast(self.state_msg())
            await self.hub.after(u, new)
        elif t == 'auto':
            p = self.players.get(u.uid) or self.nextq.get(u.uid)
            if p:
                try:
                    a = float(msg.get('at')) if msg.get('at') else None
                except (TypeError, ValueError):
                    a = None
                p['auto'] = min(a, 200.0) if a and a >= 1.1 else None

    async def on_leave(self, user):
        p = self.players.get(user.uid)
        changed = False
        if self.phase == 'wait' and p and p['state'] == 'active':
            del self.players[user.uid]
            user.balance += p['stake']
            changed = True
        if user.uid in self.nextq:
            user.balance += self.nextq.pop(user.uid)['stake']
            changed = True
        if changed:
            self.hub.store.save(user)
            await self.broadcast(self.state_msg())
            await self.hub.push_me(user)

    async def crash(self, t):
        self.phase, self.crash_at = 'crash', t
        self.hist.insert(0, self.cp)
        del self.hist[14:]
        for b in self.bots:
            if b['state'] == 'active':
                b['state'] = 'lost'
        touched = []
        for p in self.players.values():
            if p['state'] == 'active':
                p['state'] = 'lost'
                u = p['u']
                u.games += 1
                new = [k for k in ('first',) if unlock(u, k)]
                self.hub.store.save(u)
                touched.append((u, new))
        await self.broadcast(self.state_msg())
        for u, new in touched:
            await self.hub.after(u, new)

    async def run(self):
        await self.broadcast(self.state_msg())
        while True:
            await asyncio.sleep(0.05)
            t = now()
            if self.phase == 'wait' and t >= self.deadline:
                self.phase, self.t0, self.cp = 'fly', t, crash_point()
                await self.broadcast(self.state_msg())
            elif self.phase == 'fly':
                m = math.exp(R_GROWTH * (t - self.t0))
                if m >= self.cp:
                    await self.crash(t)
                    continue
                changed, touched = False, []
                for b in self.bots:
                    if b['state'] == 'active' and m >= b['target']:
                        gross = math.floor(b['stake'] * b['target'])
                        fee = fee_on(gross, b['stake'])
                        b.update(state='cashed', at=round(b['target'], 2), payout=gross - fee, fee=fee)
                        changed = True
                for p in self.players.values():
                    if p['state'] == 'active' and p['auto'] and m >= p['auto']:
                        touched.append(self.settle_cash(p, p['auto']))
                        changed = True
                if changed:
                    await self.broadcast(self.state_msg())
                    for u, new in touched:
                        await self.hub.after(u, new)
                elif t - self.last_tick > 0.4:
                    self.last_tick = t
                    await self.broadcast({'t': 'r_tick', 'm': round(m, 3)})
            elif self.phase == 'crash' and t - self.crash_at > R_CRASH_PAUSE:
                self.new_round()
                await self.broadcast(self.state_msg())


# ---------------------------------------------------------------- Хоккей

def pub_h(e):
    return {'id': e['id'], 'uid': e['uid'], 'name': e['name'], 'stake': e['stake'], 'team': e['team'], 'bot': e['bot']}


class HockeyRoom(Room):
    def __init__(self, hub, mode):
        super().__init__(hub)
        self.mode = mode
        self.new_lobby()

    def new_lobby(self):
        self.phase, self.deadline = 'lobby', now() + H_LOBBY
        self.entries, self.next_id, self.finals, self.pay, self.win_team = [], 1, [], {}, None
        names = random.sample(NAMES, random.randint(2, 4))
        self.joins = sorted(((now() + random.uniform(0.5, 10.0), n, random.randint(1, 20) * 10, 'A' if i % 2 else 'B')
                             for i, n in enumerate(names)), key=lambda x: x[0])

    def add_entry(self, name, stake, team, user=None, bot=False):
        e = {'id': self.next_id, 'uid': user.uid if user else f'bot{self.next_id}', 'u': user, 'name': name,
             'stake': stake, 'team': team, 'bot': bot}
        self.next_id += 1
        self.entries.append(e)
        return e

    def pot(self):
        return sum(e['stake'] for e in self.entries)

    def state_msg(self):
        t = now()
        m = {'t': 'h_state', 'mode': self.mode, 'phase': self.phase, 'n': len(self.conns),
             'entries': [pub_h(e) for e in self.entries], 'pot': self.pot()}
        if self.phase == 'lobby':
            m['in'] = int(max(0, self.deadline - t) * 1000)
        elif self.phase == 'play':
            m['el'] = int((t - self.play_at) * 1000)
            m['finals'] = [e['id'] for e in self.finals]
        else:
            m['el'] = int((t - self.result_at) * 1000)
            m['finals'] = [e['id'] for e in self.finals]
            m['pay'] = self.pay
        return m

    async def act(self, conn, t, msg):
        u = conn.user
        if self.phase != 'lobby':
            return
        mine = next((e for e in self.entries if e['uid'] == u.uid), None)
        if t == 'bet':
            if mine:
                return
            amt = parse_amount(msg, u)
            if amt is None:
                return await conn.send({'t': 'msg', 'text': 'Не хватает тако'})
            team = msg.get('team') if msg.get('team') in ('A', 'B') and self.mode == 'team' else 'A'
            u.balance -= amt
            self.hub.store.save(u)
            self.add_entry(u.name, amt, team, user=u)
        elif t == 'cancel':
            if not mine:
                return
            self.entries.remove(mine)
            u.balance += mine['stake']
            self.hub.store.save(u)
        else:
            return
        await self.broadcast(self.state_msg())
        await self.hub.push_me(u)

    async def on_leave(self, user):
        if self.phase != 'lobby':
            return
        mine = next((e for e in self.entries if e['uid'] == user.uid), None)
        if mine:
            self.entries.remove(mine)
            user.balance += mine['stake']
            self.hub.store.save(user)
            await self.broadcast(self.state_msg())
            await self.hub.push_me(user)

    async def begin_play(self):
        while len(self.entries) < 2:
            self.add_entry(random.choice(NAMES), random.randint(1, 20) * 10, random.choice('AB'), bot=True)
        es = self.entries
        if self.mode == 'ffa':
            r = random.random() * self.pot()
            w = es[-1]
            for e in es:
                r -= e['stake']
                if r < 0:
                    w = e
                    break
            self.finals = [w]
        else:
            sa = sum(e['stake'] for e in es if e['team'] == 'A')
            sb = sum(e['stake'] for e in es if e['team'] == 'B')
            self.win_team = 'A' if (not sb or (sa and random.random() * (sa + sb) < sa)) else 'B'
            self.finals = [e for e in es if e['team'] == self.win_team]
        self.phase, self.play_at = 'play', now()
        await self.broadcast(self.state_msg())

    async def finish(self):
        total = self.pot()
        fin_stake = sum(e['stake'] for e in self.finals)
        fin_ids = {e['id'] for e in self.finals}
        touched = []
        for e in self.entries:
            u = e['u']
            won = e['id'] in fin_ids
            gross = math.floor(total * e['stake'] / fin_stake) if won else 0
            fee = fee_on(gross, e['stake']) if won else 0
            if won and not e['bot']:
                self.pay[e['uid']] = {'gross': gross, 'fee': fee, 'net': gross - fee}
            if not u:
                continue
            u.games += 1
            new = []
            if won:
                net = gross - fee
                u.balance += net
                u.wins += 1
                u.best = max(u.best, net)
                u.total_won += max(0, net - e['stake'])
                for k, ok in (('goal', True), ('team', self.mode == 'team'), ('rich', u.balance >= 5000)):
                    if ok and unlock(u, k):
                        new.append(k)
            self.hub.store.save(u)
            touched.append((u, new))
        self.phase, self.result_at = 'result', now()
        await self.broadcast(self.state_msg())
        for u, new in touched:
            await self.hub.after(u, new)

    async def run(self):
        await self.broadcast(self.state_msg())
        while True:
            await asyncio.sleep(0.1)
            t = now()
            if self.phase == 'lobby':
                changed = False
                while self.joins and self.joins[0][0] <= t:
                    _, n, s, team = self.joins.pop(0)
                    self.add_entry(n, s, team, bot=True)
                    changed = True
                if changed:
                    await self.broadcast(self.state_msg())
                if t >= self.deadline:
                    await self.begin_play()
            elif self.phase == 'play' and t - self.play_at >= H_PLAY:
                await self.finish()
            elif self.phase == 'result' and t - self.result_at >= H_RESULT:
                self.new_lobby()
                await self.broadcast(self.state_msg())


# ---------------------------------------------------------------- хаб

class Hub:
    def __init__(self, token, db_path='arcade.db', allow_guest=False):
        self.token, self.allow_guest = token, allow_guest
        self.store = Store(db_path)
        self.rocket = RocketRoom(self)
        self.hockey = {'ffa': HockeyRoom(self, 'ffa'), 'team': HockeyRoom(self, 'team')}
        self.tasks = []

    def start(self):
        for room in (self.rocket, *self.hockey.values()):
            self.tasks.append(asyncio.ensure_future(room.run()))

    async def push_me(self, u):
        await asyncio.gather(*(c.send(u.public()) for c in list(u.conns)), return_exceptions=True)

    async def after(self, u, new_ach):
        await self.push_me(u)
        for k in new_ach:
            await asyncio.gather(*(c.send({'t': 'ach', 'key': k}) for c in list(u.conns)), return_exceptions=True)

    async def handle(self, conn, raw):
        if not conn.allow():
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        t = msg.get('t')
        if t == 'hello':
            return await self.on_hello(conn, msg)
        u = conn.user
        if not u:
            return
        if t == 'join':
            game = msg.get('game')
            room = self.rocket if game == 'rocket' else self.hockey.get(msg.get('mode')) if game == 'hockey' else None
            if not room:
                return
            if conn.room:
                await conn.room.leave(conn)
            await room.join(conn)
            await conn.send(u.public())
        elif t == 'leave':
            if conn.room:
                await conn.room.leave(conn)
        elif t in ('bet', 'cancel', 'cash', 'auto'):
            if conn.room:
                await conn.room.act(conn, t, msg)
        elif t == 'bonus':
            b = u.bonus()
            if b['claimed']:
                return
            u.last_bonus, u.streak = day_key(), b['next']
            u.balance += b['amount']
            new = [k for k, ok in (('streak3', u.streak >= 3), ('rich', u.balance >= 5000)) if ok and unlock(u, k)]
            self.store.save(u)
            await self.after(u, new)
            await conn.send({'t': 'msg', 'text': f'+{b["amount"]} 🌮'})
        elif t == 'leaders':
            rows = [{'r': i + 1, 'n': clean_name(n), 's': s, 'me': uid == u.uid} for i, (uid, n, s) in enumerate(self.store.top(10))]
            if not any(r['me'] for r in rows):
                rows.append({'r': self.store.rank(u), 'n': u.name, 's': u.total_won, 'me': True})
            await conn.send({'t': 'leaders', 'rows': rows})
        elif t == 'reset':
            if u.balance >= RESET_BELOW:
                return await conn.send({'t': 'msg', 'text': f'Доступно, когда осталось меньше {RESET_BELOW} 🌮'})
            u.balance = START_BALANCE
            self.store.save(u)
            await self.push_me(u)

    async def on_hello(self, conn, msg):
        if conn.user:
            return
        info = verify_init(str(msg.get('init') or ''), self.token)
        if info and 'id' in info:
            uid, name = f"tg{info['id']}", clean_name(info.get('first_name') or info.get('username'))
        elif self.allow_guest:
            uid, name = 'g' + re.sub(r'[^a-zA-Z0-9]', '', str(msg.get('guest') or 'x'))[:32], clean_name(msg.get('name'))
        else:
            await conn.send({'t': 'err', 'code': 'auth', 'text': 'Не удалось войти'})
            return await conn.ws.close()
        conn.user = self.store.get(uid, name)
        conn.user.conns.add(conn)
        await conn.send(conn.user.public())

    async def on_close(self, conn):
        if conn.room:
            await conn.room.leave(conn)
        if conn.user:
            conn.user.conns.discard(conn)


# ---------------------------------------------------------------- aiohttp

def make_app(hub, index_path='index.html'):
    from aiohttp import web, WSMsgType

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096)
        await ws.prepare(request)
        conn = Conn(ws)
        try:
            async for m in ws:
                if m.type == WSMsgType.TEXT:
                    await hub.handle(conn, m.data)
                elif m.type == WSMsgType.ERROR:
                    break
        finally:
            await hub.on_close(conn)
        return ws

    async def index(request):
        return web.FileResponse(index_path, headers={'Cache-Control': 'no-store'})

    async def health(request):
        return web.Response(text='ok')

    app = web.Application()
    app.router.add_get('/ws', ws_handler)
    app.router.add_get('/health', health)
    app.router.add_get('/', index)
    return app
         if __name__ == '__main__':
    from aiohttp import web

    token = os.environ.get('BOT_TOKEN')
    if not token:
        raise RuntimeError('BOT_TOKEN is not set')

    hub = Hub(token)
    hub.start()

    app = make_app(hub)

    web.run_app(
        app,
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 10000))
    )
