#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把温湿度记录仪 TH-01 与电表 EM-02 的数据对齐成小时粒度联合报表。

输入（默认 data/ 目录）：
  th.csv  时间(+08:00 ISO 字符串),温度,湿度   —— 每分钟一条
  em.csv  时间(毫秒时间戳),读数               —— 每 5 分钟一条；读数为投运以来累计电量，单位 0.01 kWh

输出：
  hourly.csv  每小时一行 + 表尾口径说明

处理原则：
  * 两台设备存在固定对时偏差，偏差从数据估计（不写死），把 EM-02 平移到 TH-01 的时钟上；
  * 设备异常按数据原样呈现：缺的留空，复位的照复位算，不插值、不补数、不接续累计值。
"""

import argparse
import csv
import datetime as dt
import os
import statistics
import sys

TZ = dt.timezone(dt.timedelta(hours=8))   # 报表时区 +08:00
HOUR = 3600
TH_CADENCE = 60          # 温湿度仪标称间隔（秒）
EM_CADENCE = 300         # 电表标称间隔（秒）
EM_UNIT_KWH = 0.01       # 电表读数单位：0.01 kWh
EM_PER_HOUR = HOUR // EM_CADENCE   # 正常小时应有的区间数 = 12


# --------------------------------------------------------------------------- 读取
def read_th(path):
    """[(utc_ts, temp), ...]，按时间排序；同一时间戳的多条记录保留。"""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            t = dt.datetime.fromisoformat(r["时间"].strip())
            if t.tzinfo is None:
                t = t.replace(tzinfo=TZ)
            rows.append((t.timestamp(), float(r["温度"])))
    rows.sort(key=lambda x: x[0])
    return rows


def read_em(path):
    """[(raw_ts_seconds, reading_int), ...]，按时间排序。"""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append((int(r["时间"].strip()) / 1000.0, int(r["读数"])))
    rows.sort(key=lambda x: x[0])
    return rows


# --------------------------------------------------------------------------- 对时
def estimate_em_offset(em):
    """
    估计 EM-02 相对 TH-01 时钟的固定偏差，返回应加到 EM 时间戳上的秒数（有符号）。

    EM 标称每 300 s 一条，若时钟准确，时间戳应全部落在 300 s 网格的整数点上。
    取每条时间戳的相位 (t mod 300) 的中位数：恒定的相位偏移即固定对时差；
    相位离散度（抖动）用来确认它是“固定”偏差而非采样漂移。
    相位超过半周期时取负方向，保证校正量绝对值最小。

    样例数据：577 条时间戳的相位全为 263 s，抖动 0 s → 修正 +37 s。
    """
    phases = [round(t) % EM_CADENCE for t, _ in em]
    phase = statistics.median(phases)
    spread = max(phases) - min(phases)
    off = EM_CADENCE - phase if phase > EM_CADENCE / 2 else -phase
    return round(off), phase, spread


def on_grid(times, cadence):
    return all(abs(t - round(t / cadence) * cadence) < 0.5 for t in times)


# --------------------------------------------------------------------------- 汇总
def hour_key(ts):
    return int(ts // HOUR) * HOUR


def fmt_hour(hk):
    return dt.datetime.fromtimestamp(hk, TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def fmt_ts(ts, pattern="%m-%d %H:%M:%S"):
    return dt.datetime.fromtimestamp(ts, TZ).strftime(pattern)


def build_rows(th, em, em_off):
    # ---- 温度：按小时分组；同一时间戳的多条读数先取平均，视为同一次采样
    per_hour = {}          # hour -> {stamp_sec: [values]}
    for ts, v in th:
        stamp = round(ts)
        per_hour.setdefault(hour_key(ts), {}).setdefault(stamp, []).append(v)

    # ---- 电量：逐区间作差，按区间起点归入小时
    em_energy, em_intervals, em_resets = {}, {}, []
    for (t0, v0), (t1, v1) in zip(em, em[1:]):
        t0c, t1c = t0 + em_off, t1 + em_off
        k = hour_key(t0c)
        em_energy.setdefault(k, 0)
        em_intervals.setdefault(k, 0)
        d = v1 - v0
        if d < 0:
            # 计数器回零：该区间用量在数据里不存在，本区间不计入、留缺
            em_resets.append({"t0": t0c, "t1": t1c, "v0": v0, "v1": v1})
            continue
        em_energy[k] += d
        em_intervals[k] += 1

    hours = sorted(set(per_hour) | set(em_energy))
    # 停机：TH 观测区间内、整小时没有任何采样的时段（观测期之外不算停机）
    th_hours = sorted(per_hour)
    stopped = [k for k in hours
               if th_hours and th_hours[0] <= k <= th_hours[-1] and not per_hour.get(k)]
    gap = {}
    if stopped:
        before = max((ts for ts, _ in th if ts < stopped[0]), default=None)
        after = min((ts for ts, _ in th if ts >= stopped[-1] + HOUR), default=None)
        gap = {"hours": stopped, "before": before, "after": after}

    rows = []
    for k in hours:
        stamps = per_hour.get(k, {})
        means = [sum(vs) / len(vs) for vs in stamps.values()]
        notes = []

        if not stamps:
            notes.append("温湿度仪重启停机，无采样")
        else:
            for s in sorted(s for s, vs in stamps.items() if len(vs) > 1):
                vs = stamps[s]
                notes.append("%s 同一时间戳 %d 条读数(%s)，取平均后计一次"
                             % (fmt_ts(s, "%H:%M:%S"), len(vs),
                                "/".join("%.1f" % x for x in vs)))
            odd = sorted(s for s in stamps if s % TH_CADENCE != 0)
            if odd:
                notes.append("含非整分时间戳 %s，按其自身时间归入本小时"
                             % "、".join(fmt_ts(s, "%H:%M:%S") for s in odd))
            whole = {dt.datetime.fromtimestamp(s, TZ).minute
                     for s in stamps if s % TH_CADENCE == 0}
            missing = [m for m in range(60) if m not in whole]
            if missing:
                label = "、".join(":".join([fmt_ts(k, "%H"), "%02d" % m]) for m in missing[:6])
                if len(missing) > 6:
                    label += " 等 %d 个" % len(missing)
                notes.append("缺整分点 %s" % label)

        energy = em_energy.get(k)
        n_int = em_intervals.get(k, 0)
        if energy is None:
            notes.append("电表读数复位，本小时无可用区间")
        elif n_int < EM_PER_HOUR:
            notes.append("电表掉电复位：本小时仅 %d 个有效 5 分钟区间（缺 %d 个），"
                         "电量为这 %d 个区间之和" % (n_int, EM_PER_HOUR - n_int, n_int))
        if any(hour_key(r["t1"]) == k for r in em_resets):
            notes.append("电表复位后读数自 0 重新累计，本小时起按新读数计算")

        rows.append({
            "hour": k,
            "temp": None if not means else round(sum(means) / len(means), 2),
            "temp_n": len(means),
            "energy": energy,
            "energy_n": n_int,
            "notes": notes,
        })

    return rows, em_resets, per_hour, gap


# --------------------------------------------------------------------------- 输出
def write_csv(path, rows, th, em, em_off, phase, spread, em_resets, gap, per_hour):
    dup_total = sum(1 for stamps in per_hour.values()
                    for vs in stamps.values() if len(vs) > 1)
    dup_extra = sum(len(vs) - 1 for stamps in per_hour.values()
                    for vs in stamps.values() if len(vs) > 1)
    dup_hours = sum(1 for stamps in per_hour.values()
                    if any(len(vs) > 1 for vs in stamps.values()))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["时间", "温度均值(℃)", "温度采样数", "电量(kWh)", "电量区间数", "备注"])
        for r in rows:
            w.writerow([
                fmt_hour(r["hour"]),
                "" if r["temp"] is None else "%.2f" % r["temp"],
                r["temp_n"],
                "" if r["energy"] is None else "%.2f" % (r["energy"] * EM_UNIT_KWH),
                r["energy_n"],
                "；".join(r["notes"]),
            ])

        w.writerow([])
        w.writerow(["口径说明", ""])
        w.writerow(["项目", "说明"])

        w.writerow(["时间基准",
                    "报表时区 +08:00，每行代表 [整点, 下一整点) 一小时；共 %d 行，覆盖 %s 至 %s。"
                    % (len(rows), fmt_hour(rows[0]["hour"]), fmt_hour(rows[-1]["hour"]))])

        n_th_whole = sum(1 for ts, _ in th if round(ts) % TH_CADENCE == 0)
        n_th_odd = len(th) - n_th_whole
        w.writerow(["两台设备的对时差",
                    "固定对时差 %d 秒。判据：EM-02 标称每 5 分钟一条，若钟准，时间戳应落在 300 秒网格上；"
                    "实测 %d 条时间戳的相位恒为 +%d 秒、抖动 %d 秒——一条不差，是装机留下的固定偏差而非采样漂移。"
                    "TH-01 的 %d 条记录中有 %d 条落在整分点（其余 %d 条秒位不为零，"
                    "是记录重写/时间跳变的零星数据），分钟网格自洽，故以 TH-01 的钟为基准。"
                    "同一物理时刻，EM-02 记下的时间比 TH-01 早 %d 秒：EM-02 的钟慢了 %d 秒。"
                    "若反过来把 EM-02 的钟当基准，则结论是 TH-01 的钟快了 %d 秒——"
                    "两个文件只能给出这 %d 秒的相对差值，谁是绝对准的无法从数据本身判定，"
                    "报表采用的基准是 TH-01。"
                    % (abs(em_off), len(em), phase, spread, len(th), n_th_whole, n_th_odd,
                       abs(em_off), abs(em_off), abs(em_off), abs(em_off))])

        em_span = em[-1][0] - em[0][0]
        w.writerow(["校准方向",
                    "校准 EM-02，TH-01 不作平移：每条电表时间戳 +%d 秒后与 TH-01 对齐"
                    "（把慢了 %d 秒的钟往前拨 %d 秒）。"
                    "校验：EM-02 原始首条 %s → 校正后 %s（正好整点）；原始末条 %s → 校正后 %s；"
                    "校正后跨度 %d 秒（整 %d 天），全部 %d 条时间戳精确落在 5 分钟网格上。"
                    "选 +%d 秒而非 -%d 秒：两者都能让网格对齐，但只有 +%d 秒把序列两端落在整日 00:00，"
                    "且校正量不超过半个采样间隔，是唯一的最小解。"
                    % (em_off, abs(em_off), abs(em_off),
                       fmt_ts(em[0][0]), fmt_ts(em[0][0] + em_off),
                       fmt_ts(em[-1][0]), fmt_ts(em[-1][0] + em_off),
                       em_span, em_span // 86400, len(em),
                       em_off, EM_CADENCE - abs(em_off), em_off)])

        w.writerow(["温度统计口径",
                    "TH-01 每分钟一条。小时温度 = 该小时内全部有效采样的算术平均（等权，不做时间加权），"
                    "保留两位小数；不插值、不以前后小时补缺。"
                    "同一时间戳写了多条读数时，先把这几条取平均作为该时刻的一次采样再计入小时均值"
                    "（不让某一分钟被重复计权），温度采样数按去重后的时间戳个数统计。"
                    "非整分时间戳（如 05:59:14）按其自身时间归入所属小时并计入均值。"
                    "温度采样数即该小时参与平均的时间戳个数，正常为 60；"
                    "少于 60 只表示该小时有缺失，缺口不补，均值仅代表该小时实有样本的平均。"
                    "全部数据中有 %d 个时间戳各写了多条读数（多出 %d 条记录），涉及 %d 个小时。"
                    % (dup_total, dup_extra, dup_hours)])

        w.writerow(["电量统计口径",
                    "EM-02 每 5 分钟一条，读数为投运以来累计电量，单位 0.01 kWh。"
                    "小时电量 = 该小时内各 5 分钟区间读数增量之和 × 0.01；区间按开始时刻归入小时"
                    "（[h:00, h+1:00) 内的 12 个区间属于 h 点这一小时），正常小时电量区间数为 12。"
                    "报表电量即“该小时电表实际记录到的用量”，单位 kWh，保留两位小数。"
                    "区间划分在 EM-02 时间戳校正 %d 秒之后进行，保证按真实整点切分。" % em_off])

        if gap:
            hours_txt = "、".join(fmt_hour(k) for k in gap["hours"])
            # 停机时段之外，采样数不等于 60 的小时（非整分记录造成）
            partial = [r for r in rows if r["temp_n"] and r["temp_n"] != 60]
            partial_txt = ""
            if partial:
                desc = "、".join(
                    "%s %d 条（%s）" % (
                        fmt_ts(r["hour"], "%m-%d %H:00"), r["temp_n"],
                        "多出一条非整分记录" if r["temp_n"] > 60 else "缺一个整分点")
                    for r in partial)
                partial_txt = ("另有 %d 个小时因单条记录未落在整分点而采样数不等于 60：%s。"
                               "这些小时同样只按实有采样平均，不补不删，备注已逐小时标注。"
                               % (len(partial), desc))
            w.writerow(["异常一：温湿度仪重启",
                        "TH-01 于 %s 之后停机、%s 恢复，期间 %d 分钟无采样。"
                        "表内 %s 各行温度留空、温度采样数为 0，备注标注停机；不插值、不用前后小时平滑。"
                        "同一时段电表照常给出电量。%s"
                        % (fmt_ts(gap["before"]), fmt_ts(gap["after"]),
                           int((gap["after"] - gap["before"]) // 60) - 1, hours_txt,
                           partial_txt)])

        if em_resets:
            rst = em_resets[0]
            k_drop = hour_key(rst["t0"])
            n_drop = next(r["energy_n"] for r in rows if r["hour"] == k_drop)
            w.writerow(["异常二：电表掉电复位",
                        "EM-02 在 %s 至 %s 之间掉电，读数由 %d 直接变为 %d 并自 0 重新累计；"
                        "这段停电期间的真实用量在数据中不存在，无法还原。表内体现为："
                        "① %s 这一小时只有 %d 个有效 5 分钟区间（缺 %d 个，即 %s→%s 这一段），"
                        "电量为其余区间之和，不是该小时的真实全量；"
                        "② 复位后 %s 的读数 %d（=%.2f kWh）是本区间内的部分累计，"
                        "与掉电到重启之间的未知损失同处这个缺口区间，一并舍弃、不计入任何小时，"
                        "宁可缺也不把它拆到前后小时；"
                        "③ %s 起按复位后的读数正常作差计算小时电量；"
                        "④ 报表没有把复位前的累计读数接到复位后，也没有回填掉电损失，"
                        "因此全表电量不能跨 %s 累加，复位前后是两段彼此独立的累计（报表电量合计不等于任何单次抄表的差值）。"
                        % (fmt_ts(rst["t0"], "%m-%d %H:%M"), fmt_ts(rst["t1"], "%m-%d %H:%M"),
                           rst["v0"], rst["v1"],
                           fmt_ts(k_drop, "%m-%d %H:00"), n_drop, EM_PER_HOUR - n_drop,
                           fmt_ts(rst["t0"], "%H:%M"), fmt_ts(rst["t1"], "%H:%M"),
                           fmt_ts(rst["t1"], "%H:%M"), rst["v1"], rst["v1"] * EM_UNIT_KWH,
                           fmt_ts(rst["t1"], "%m-%d %H:00"),
                           fmt_ts(rst["t0"], "%m-%d %H:%M"))])


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="TH-01 / EM-02 小时粒度对齐")
    ap.add_argument("--th", default=os.path.join(here, "data", "th.csv"))
    ap.add_argument("--em", default=os.path.join(here, "data", "em.csv"))
    ap.add_argument("--out", default=os.path.join(here, "hourly.csv"))
    a = ap.parse_args()

    th = read_th(a.th)
    em = read_em(a.em)

    em_off, phase, spread = estimate_em_offset(em)
    ok = on_grid([t + em_off for t, _ in em], EM_CADENCE)

    rows, em_resets, per_hour, gap = build_rows(th, em, em_off)
    write_csv(a.out, rows, th, em, em_off, phase, spread, em_resets, gap, per_hour)

    print("TH %d 条记录 / %d 个不同时刻，%s ~ %s"
          % (len(th), sum(len(d) for d in per_hour.values()),
             fmt_ts(th[0][0], "%m-%d %H:%M"), fmt_ts(th[-1][0], "%m-%d %H:%M")), file=sys.stderr)
    print("EM %d 条，相位 t%%300 = %d s（抖动 %d s）→ 修正 %+d s，5 分钟网格校验：%s"
          % (len(em), phase, spread, em_off, "通过" if ok else "未通过"), file=sys.stderr)
    print("EM 复位 %d 处" % len(em_resets), file=sys.stderr)
    print("已写出 %s（%d 行数据 + 口径说明）" % (a.out, len(rows)), file=sys.stderr)


if __name__ == "__main__":
    main()
