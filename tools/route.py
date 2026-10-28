#!/usr/bin/env python3
"""甄嬛军师 · 不接大模型的路由器。

输入一句职场情形，用关键词判断场合和局面类型，在 references/场景映射.md 的约 125 行里
按字面相似度检索最像的局，再从局卡和招式库里取出打法、能直接说的话和雷区，拼成回复。

用法：
  python3 route.py "明天晋升答辩，两位评委一个管运营一个管社区"
  python3 route.py --debug "……"     # 同时打印路由过程和候选得分
  python3 route.py --json "……"      # 输出结构化结果，给大模型当检索结果用
"""
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REF = os.path.join(HERE, '..', 'references')

# ---------- 路由规则：信号词 → 场合 / 局面 / 对方 ----------
SCENES = {
    '面试': ['面试', '面试官', '终面', '二面', '一面', '答辩', '评委', '述职', '背调', 'HR', 'hr', '简历', '应聘'],
    '聊投资': ['投资人', '路演', '融资', '估值', 'BP', '尽调', '投资', 'VC', '天使'],
    '老板 1on1': ['老板', '领导', '上级', '上司', '主管', '加薪', '涨薪', '晋升', '升职', '1on1', '汇报', '绩效', '资源', '预算', '编制'],
    '和行业大佬建联': ['大佬', '展会', '饭局', '加微信', '建联', '酒会', '前辈', '引荐', '介绍认识', '搭话', '行业'],
    '被当众发难': ['当众', '会上', '质疑', '发难', '挑刺', '怼', '拆台', '刁难', '当着', '开会', '追问', '挑毛病', '复盘会', '当面'],
    '处境不利': ['背锅', '冷落', '边缘', '排挤', '失误', '出错', '被抢', '抢功', '甩锅', '不利', '被怀疑', '降级', '架空', '空降', '拆给', '拆走', '锅', '事故', '夺权'],
    '投其所好制造契机': ['送礼', '投其所好', '喜好', '拉近', '示好', '讨好', '契机', '刷存在感'],
    'small talk': ['冷场', '寒暄', '闲聊', 'small talk', '尬聊', '开场', '第一次见', '电梯', '没话说'],
}
SITUATIONS = {
    '被当众发难': ['质疑', '发难', '挑刺', '怼', '拆台', '当众', '刁难', '追问', '挑毛病', '复盘会', '当面', '甩给', '推给'],
    '被试探': ['试探', '考你', '考验', '问你怎么看', '摸底', '答辩', '评委', '面试'],
    '求人办事': ['加薪', '涨薪', '晋升', '资源', '求他', '求人', '请他帮', '引荐', '争取', '要预算'],
    '被抢功陷害': ['抢功', '甩锅', '背锅', '陷害', '冒名', '告状', '锅', '推卸', '责任推', '架空', '拆给', '抢项目', '说成是他的'],
    '拒绝': ['拒绝', '推掉', '不想接', '婉拒', '回绝', '不想去', '推辞', '邀请我', '邀约', '拉我', '挖我', '叫我一起', '跟着他', '不想跟'],
    '谈条件': ['谈条件', '谈判', '报价', '开价', '让步', '筹码'],
    '投其所好': ['投其所好', '送礼', '喜好', '拉近'],
    '闲谈社交': ['寒暄', '闲聊', '冷场', '饭局', 'small talk', '搭话'],
    '认错善后': ['道歉', '认错', '搞砸', '补救', '失误', '撕破脸', '翻脸', '闹僵'],
    '立威管人': ['下属', '带团队', '管人', '新人不服', '立规矩'],
}
COUNTERPARTS = [('VP', '上位'), ('总监', '上位'), ('合伙人', '上位'), ('旧上司', '上位'), ('前老板', '上位'), ('老领导', '上位'), ('前领导', '上位'), ('评委', '上位'), ('面试官', '上位'), ('投资人', '上位'), ('老板的老板', '上位'), ('老板', '上位'),
                ('领导', '上位'), ('上级', '上位'), ('主管', '上位'), ('大佬', '上位'), ('前辈', '上位'),
                ('同事', '平级'), ('平级', '平级'), ('合作方', '平级'), ('负责人', '平级'), ('部门', '平级'), ('下属', '下位'), ('新人', '下位')]

STOP = set('的了是在我你他她们这那就都也和与及或一个有没不很要会能吧呢啊吗嘛么去来说到着把被让给对从为以')


def bigrams(text):
    s = re.sub(r'[\s\W_]+', '', text)
    grams = {s[i:i + 2] for i in range(len(s) - 1)}
    return {g for g in grams if not (g[0] in STOP and g[1] in STOP)}


def strip_md(text):
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    return text.replace('**', '').strip()


# ---------- 读 references ----------
def load_rows():
    rows, section, top = [], None, None
    for line in open(os.path.join(REF, '场景映射.md'), encoding='utf-8'):
        if line.startswith('## '):
            top = line[3:].strip()
            if top.startswith('暂不能映射') or top.startswith('局面类型速查表'):
                section = None
            continue
        if line.startswith('### '):
            section = re.sub(r'^\d+\.\s*', '', line[4:].strip())
            section = re.sub(r'（.*?）', '', section).strip()
            continue
        if not section or not line.startswith('| ') or line.startswith('| 情形') or line.startswith('|---'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split(' | ')]
        if len(cells) < 6:
            continue
        m = re.search(r'\]\((局卡/[^)]+\.md)\)', cells[1])
        rows.append(dict(section=section, situation=strip_md(cells[0]), card=m.group(1) if m else None,
                         story=strip_md(cells[1]), moves=strip_md(cells[2]), logic=strip_md(cells[3]),
                         avoid=strip_md(cells[4]), low='低优先级' in line))
    return rows


def load_type_cards():
    """局面类型速查表：局面类型 → 局卡路径列表。"""
    types, on = {}, False
    for line in open(os.path.join(REF, '场景映射.md'), encoding='utf-8'):
        if line.startswith('## 局面类型速查表'):
            on = True
            continue
        if on and line.startswith('| ') and not line.startswith('| 局面类型') and '|---' not in line:
            cells = [c.strip() for c in line.strip().strip('|').split(' | ')]
            if len(cells) >= 2:
                types[cells[0]] = re.findall(r'\]\((局卡/[^)]+\.md)\)', cells[1])
    return types


def load_moves():
    moves, name = {}, None
    for line in open(os.path.join(REF, '招式库.md'), encoding='utf-8'):
        m = re.match(r'^## \d+\.\s*(.+?)（', line)
        if m:
            name = m.group(1).strip()
            continue
        if name and line.startswith('一句话：'):
            moves[name] = line[4:].strip()
            name = None
    return moves


def load_card_lines(card):
    """取局卡“换到职场”里每个场合的一句话。"""
    path = os.path.join(REF, card)
    if not os.path.exists(path):
        return []
    text = open(path, encoding='utf-8').read()
    part = text.split('## 5. 换到职场', 1)[-1].split('## 雷区', 1)[0]
    out = []
    for block in re.split(r'\n### ', part)[1:]:
        head = block.split('\n', 1)[0]
        m = re.search(r'一句能直接说出口的话：\s*(.+)', block)
        if m:
            out.append((head, m.group(1).strip()))
    return out


# ---------- 路由 ----------
def classify(query):
    scene_hits = {k: sum(w in query for w in ws) for k, ws in SCENES.items()}
    scenes = [k for k, v in sorted(scene_hits.items(), key=lambda x: -x[1]) if v > 0]
    sit_hits = {k: sum(w in query for w in ws) for k, ws in SITUATIONS.items()}
    situations = [k for k, v in sorted(sit_hits.items(), key=lambda x: -x[1]) if v > 0]
    counterpart = next(((w, lvl) for w, lvl in COUNTERPARTS if w in query), (None, None))
    return scenes, situations, counterpart


def route(query, top_k=3):
    rows, moves, types = load_rows(), load_moves(), load_type_cards()
    scenes, situations, counterpart = classify(query)
    primary = list(types.get(situations[0], [])) if situations else []
    secondary = [c for c in (types.get(situations[1], []) if len(situations) > 1 else []) if c not in primary]
    type_cards = set(primary + secondary)
    q = bigrams(query)
    scored = []
    for r in rows:
        rb = bigrams(r['situation'])
        sim = len(q & rb) / math.sqrt(len(rb) + 1) if rb else 0
        boost = 0.0
        if r['section'] in scenes:
            boost += 0.8 if r['section'] == scenes[0] else 0.4
        boost += 0.15 * sum(w in r['situation'] for s in situations for w in SITUATIONS[s])
        if r['card'] in primary:
            boost += 0.5
        elif r['card'] in secondary:
            boost += 0.2
        score = (sim + boost) * (0.6 if r['low'] else 1.0)
        scored.append((score, sim, r))
    scored.sort(key=lambda x: -x[0])
    picks = []
    for score, sim, r in scored:
        if r['card'] and r['card'] not in {p['card'] for p in picks}:
            p = dict(r, score=round(score, 3), sim=round(sim, 3))
            names = [n.strip() for n in re.split(r'[、，]', r['moves']) if n.strip()]
            p['move_details'] = [(n, moves.get(n, '')) for n in names[:3]]
            p['lines'] = load_card_lines(r['card'])
            picks.append(p)
        if len(picks) >= top_k:
            break
    picked = {p['card'] for p in picks}
    extra = [dict(card=c, title=os.path.basename(c)[:-3], lines=load_card_lines(c))
             for c in primary + secondary if c not in picked][:4]
    return dict(query=query, scenes=scenes, situations=situations, counterpart=counterpart[0],
                counterpart_level=counterpart[1], picks=picks, type_cards=extra)


# ---------- 不接大模型的回复拼装 ----------
SCENE_WORD = {'面试': '面试', '聊投资': '投资', '老板 1on1': '老板', '和行业大佬建联': '大佬'}


def clean(text):
    text = re.sub(r'^(局卡|样卡)[^：]*：', '', text)
    text = re.sub(r'（[^（）]*(局卡|样卡|第\d+集|幕)[^（）]*）', '', text)
    text = re.sub(r'(局卡|样卡)_?[A-Z]?\d*[^，。；：]*?里', '', text)
    return text.strip()


def first_sentence(text, limit=90):
    text = clean(text)
    cut = re.split(r'(?<=[。；])', text)
    out = ''
    for c in cut:
        if len(out) + len(c) > limit and out:
            break
        out += c
    return out.rstrip('；').replace('甄嬛', '本宫')


def pick_line(p, scenes):
    for s in scenes:
        word = SCENE_WORD.get(s)
        for head, line in p['lines']:
            if word and word in head:
                return line
    return p['lines'][0][1] if p['lines'] else None


def compose(res):
    if not res['picks']:
        return '妹妹，你说的这个局本宫一时对不上号，你且说说：你要面对的是谁，当众还是私下，你想要个什么结果？'
    top = res['picks'][0]
    who = res['counterpart'] or '对面那位'
    scene = res['scenes'][0] if res['scenes'] else top['section']
    sit = res['situations'][0] if res['situations'] else '要过关'
    out = [f'妹妹先别慌。本宫看这是一局“{scene}”里的“{sit}”，你要面对的是{who}。']
    out.append(f'本宫当年也遇过类似的：{first_sentence(top["story"])}')
    tips = [f'“{n}”，{d.split("比如")[0].rstrip("，（(")}' for n, d in top['move_details'][:2] if d]
    if tips:
        out.append('这一局用得上的招：' + '；'.join(tips) + '。')
    line = pick_line(top, res['scenes'])
    if line:
        out.append(f'到时候你就这么说：{line}')
    if top['avoid']:
        warn = re.split(r'(?<=。)', clean(top['avoid']))[0].rstrip('：:')
        out.append(f'只有一样要当心：{warn}')
    others = [f'“{p["situation"][:30]}……”' for p in res['picks'][1:]]
    if others:
        out.append('要是你的情况更像' + '，或者'.join(others) + '，跟本宫说，咱们换个打法。')
    return '\n\n'.join(out)


def main():
    args = sys.argv[1:]
    debug, as_json = '--debug' in args, '--json' in args
    query = ' '.join(a for a in args if not a.startswith('--'))
    if not query:
        print(__doc__)
        return
    res = route(query)
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return
    if debug:
        print(f'【路由】场合={res["scenes"]} 局面={res["situations"]} 对方={res["counterpart"]}({res["counterpart_level"]})')
        for p in res['picks']:
            print(f'  候选 {p["score"]}（字面{p["sim"]}）[{p["section"]}] {p["situation"][:40]} → {os.path.basename(p["card"])}')
        for t in res['type_cards']:
            print(f'  同类局卡（按局面类型）→ {t["title"]}')
        print('-' * 40)
    print(compose(res))


if __name__ == '__main__':
    main()
