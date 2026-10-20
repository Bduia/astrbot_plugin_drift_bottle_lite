import os
import json
import random
import uuid
import time
import asyncio
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Star, Context
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api import logger

class DriftBottleFull(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = os.path.join(get_astrbot_data_path(), "plugin_data")
        os.makedirs(self.data_dir, exist_ok=True)
        
        self.data_file = os.path.join(self.data_dir, "drift_bottle_full.json")
        self.blacklist_file = os.path.join(self.data_dir, "drift_bottle_blacklist.json")
        
        self.groups = {}
        self.user_cooldowns = {}
        self.group_receive_times = {} 
        
        gp_str = self.config.get("group_nicknames", "群一,群二,群三,群四,群五")
        self.group_pool = [g.strip() for g in gp_str.split(",") if g.strip()]
        self.group_aliases = {}
        
        self.bottles = self.load_json(self.data_file, [])
        self.user_blacklist = self.load_json(self.blacklist_file, [])
        
        # 核心并发锁：防止两个用户同时捞到同一个瓶子，或同时修改 JSON
        self._lock = asyncio.Lock()
        
        self._closed = False
        self.task = asyncio.create_task(self.clean())

    def load_json(self, path, default):
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def save_json(self, path, data):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception:
            pass

    async def clean(self):
        while not self._closed:
            await asyncio.sleep(60)
            lifespan = self.config.get("bottle_max_lifespan_hours", 72) * 3600
            now = time.time()
            # 定期清理过期瓶子
            valid = [b for b in self.bottles if now - b["time"] <= lifespan]
            if len(valid) != len(self.bottles):
                async with self._lock: # 加锁清理
                    self.bottles = valid
                    self.save_json(self.data_file, self.bottles)

    def get_alias(self, gid):
        gid = str(gid)
        if gid not in self.group_aliases:
            if self.group_pool:
                alias = random.choice(self.group_pool)
                self.group_pool.remove(alias)
                self.group_aliases[gid] = alias
            else:
                self.group_aliases[gid] = f"神秘海域{len(self.group_aliases)+1}"
        return self.group_aliases[gid]

    def is_white(self, gid):
        w_str = self.config.get("bottle_group_whitelist", "")
        return not w_str or str(gid) in [w.strip() for w in w_str.split(",")]

    def is_admin(self, event):
        HARDCODE_ADMIN = "你的QQ号填这里" # <--- 记得改这里
        sender_id = str(event.get_sender_id())
        admins = self.config.get("bottle_admin_ids", "").split(",")
        admin_list = [a.strip() for a in admins]
        return sender_id in admin_list or sender_id == HARDCODE_ADMIN

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def rec(self, event: AstrMessageEvent):
        gid = event.get_group_id()
        if gid:
            self.groups[str(gid)] = event.unified_msg_origin
            self.get_alias(str(gid))

    @filter.command("扔瓶")
    async def throw(self, event: AstrMessageEvent):
        if not self.config.get("bottle_enabled", True): return
        gid = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        
        if not self.is_white(gid): return
        if user_id in self.user_blacklist:
            yield event.plain_result("🚫 你已被限制使用漂流瓶功能。")
            return

        raw_text = event.message_str.replace("/扔瓶", "", 1).strip()
        if not raw_text:
            yield event.plain_result("🍶 瓶子里什么都没写，扔了个寂寞。请附上内容：/扔瓶 想说的话")
            return
            
        # 🚨 风险防御：特殊符号注入（防艾特全体、防Markdown乱码）
        text = raw_text.replace("@", "＠").replace("\n", " ").strip()

        now = time.time()
        
        # 🚨 风险防御：并发锁（扔瓶也加锁，防止内存数据错乱）
        async with self._lock:
            # 1. 个人冷却判断
            cooldown = self.config.get("bottle_cooldown_seconds", 60)
            if now - self.user_cooldowns.get(user_id, 0) < cooldown:
                remain = int(cooldown - (now - self.user_cooldowns.get(user_id, 0)))
                yield event.plain_result(f"⏳ 别急，海面风浪太大，请等待 {remain} 秒后再扔吧！")
                return

            # 2. 敏感词判断
            blacklist = self.config.get("bottle_blacklist_words", "").split(",")
            for w in blacklist:
                if w.strip() and w.strip() in text:
                    yield event.plain_result("🚫 瓶子内容包含敏感词，被海警拦截了。")
                    return

            # 🚨 风险防御：内容去重（防复读机刷屏）
            dup_hours = self.config.get("duplicate_check_hours", 24)
            if dup_hours > 0:
                for b in self.bottles:
                    if b["text"] == text and (now - b["time"]) < dup_hours * 3600:
                        yield event.plain_result("🦜 这个内容最近有人扔过了，换点新鲜的吧！")
                        return

            # 🚨 风险防御：数据爆炸限制（海面容量限制）
            max_total = self.config.get("max_total_bottles", 500)
            if len(self.bottles) >= max_total:
                yield event.plain_result("🌊 全服海面已经满溢了，请等别人捞走一些再扔吧。")
                return
                
            max_per_group = self.config.get("max_bottles_per_group", 20)

            # 3. 目标群接收滑动窗口 + 单群容量筛选
            window = self.config.get("target_group_window", 300)
            max_bottles = self.config.get("max_bottles_per_window", 2)
            
            avail_groups = []
            for g in self.groups.keys():
                if g == gid or not self.is_white(g):
                    continue
                    
                # 检查目标群海面是否已满
                group_bottle_count = sum(1 for b in self.bottles if b["target"] == g)
                if group_bottle_count >= max_per_group:
                    continue
                
                # 检查目标群时间窗口
                times = self.group_receive_times.get(g, [])
                valid_times = [t for t in times if now - t <= window]
                self.group_receive_times[g] = valid_times
                
                if len(valid_times) < max_bottles:
                    avail_groups.append(g)

            if not avail_groups:
                yield event.plain_result("🌊 海面太拥挤了，其他群刚好满额或已满容，晚点再扔吧。")
                return

            target_group = random.choice(avail_groups)
            nick_str = self.config.get("bottle_nicknames", "神秘人")
            nick = random.choice([n.strip() for n in nick_str.split(",") if n.strip()]) or "神秘人"

            max_len = self.config.get("bottle_max_length", 200)
            if len(text) > max_len: text = text[:max_len] + "..."
                
            bottle_id = uuid.uuid4().hex[:6].upper()
            bottle = {
                "id": bottle_id, "text": text, "origin": gid, "target": target_group, 
                "time": now, "count": 0, "nick": nick, "journey": [f"从{self.get_alias(gid)}投出"]
            }
            
            self.bottles.append(bottle)
            self.save_json(self.data_file, self.bottles)
            
            self.user_cooldowns[user_id] = now
            if target_group not in self.group_receive_times:
                self.group_receive_times[target_group] = []
            self.group_receive_times[target_group].append(now)

        # 🚨 风险防御：幽灵群剔除（发送失败则删群、吞瓶，不影响后续运行）
        try:
            chain = MessageChain([Plain("🌊 海上漂来一个匿名漂流瓶！快去输入 /捞瓶 看看吧！")])
            await self.context.send_message(self.groups[target_group], chain)
            yield event.plain_result(f"🍶 瓶子已扔出！你的代号是：{nick}\n(匿名投递，不会暴露你的身份)")
        except Exception as e:
            logger.error(f"[漂流瓶] 发送到群 {target_group} 失败，可能已失联: {e}")
            async with self._lock:
                self.groups.pop(target_group, None) # 剔除死群
                self.bottles = [b for b in self.bottles if b["target"] != target_group] # 收回瓶子
                self.save_json(self.data_file, self.bottles)
            yield event.plain_result("🌪️ 一阵妖风吹过，瓶子在半路沉没了，请重新扔一次吧。")

    @filter.command("捞瓶")
    async def pick(self, event: AstrMessageEvent):
        gid = str(event.get_group_id())
        if not self.is_white(gid): return

        # 🚨 风险防御：并发抢瓶锁（保证一个人捞走后，别人绝对捞不到同一个）
        async with self._lock:
            avail = [b for b in self.bottles if b["target"] == gid]
            if not avail:
                yield event.plain_result("🌊 海面上空空如也，这个群暂时没有漂流瓶。")
                return

            bottle = random.choice(avail)
            self.bottles.remove(bottle)
            bottle["count"] += 1
            bottle["journey"].append(f"被{self.get_alias(gid)}捞起")

            max_pickups = self.config.get("bottle_max_pickups", 3)
            if bottle["count"] < max_pickups:
                avail_g = [g for g in self.groups.keys() if g != gid and self.is_white(g)]
                if avail_g:
                    bottle["target"] = random.choice(avail_g)
                    self.bottles.append(bottle)
                else:
                    bottle["journey"].append("无群可漂，搁浅")
            else:
                bottle["journey"].append("完成使命，沉入海底")
                
            self.save_json(self.data_file, self.bottles)

        msg = (f"🍾 你捞起了一个漂流瓶！(第{bottle['count']}次被捞)\n"
               f"来自代号：{bottle['nick']}\n"
               f"内容：{bottle['text']}\n"
               f"旅程：{' -> '.join(bottle['journey'])}")
        yield event.plain_result(msg)

    @filter.command("漂流管理")
    async def admin(self, event: AstrMessageEvent):
        if not self.is_admin(event):
            yield event.plain_result(f"⛔ 你没有权限。你的QQ: {event.get_sender_id()}")
            return
        
        gid = str(event.get_group_id())
        args = event.message_str.replace("/漂流管理", "", 1).strip().split()
        
        if not args:
            yield event.plain_result("🛠️ 管理指令：列表 / 沉没 编号 / 清空 / 拉黑 QQ / 解封 QQ")
            return

        action = args[0]
        async with self._lock:
            if action == "列表":
                avail = [b for b in self.bottles if b["target"] == gid]
                if not avail: yield event.plain_result("🌊 当前群没有瓶子。")
                else:
                    msg = "📋 当前群的瓶子：\n"
                    for b in avail: msg += f"[{b['id']}] {b['text'][:10]}...\n"
                    yield event.plain_result(msg)
            elif action == "沉没" and len(args) > 1:
                for b in self.bottles:
                    if b["id"] == args[1].upper():
                        self.bottles.remove(b)
                        self.save_json(self.data_file, self.bottles)
                        yield event.plain_result(f"🌊 瓶子 {args[1].upper()} 已沉没。")
                        return
                yield event.plain_result("🔍 未找到该瓶子。")
            elif action == "清空":
                self.bottles = [b for b in self.bottles if b["target"] != gid]
                self.save_json(self.data_file, self.bottles)
                yield event.plain_result("🌪️ 当前群瓶子已清空。")
            elif action == "拉黑" and len(args) > 1:
                if args[1] not in self.user_blacklist:
                    self.user_blacklist.append(args[1])
                    self.save_json(self.blacklist_file, self.user_blacklist)
                    yield event.plain_result(f"🔨 QQ:{args[1]} 已拉黑。")
            elif action == "解封" and len(args) > 1:
                if args[1] in self.user_blacklist:
                    self.user_blacklist.remove(args[1])
                    self.save_json(self.blacklist_file, self.user_blacklist)
                    yield event.plain_result(f"✅ QQ:{args[1]} 已解封。")

    async def terminate(self):
        self._closed = True
        if self.task:
            self.task.cancel()