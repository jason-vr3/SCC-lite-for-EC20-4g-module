#!/usr/bin/env python3
"""PDU 接收测试环境(只读).

验证: 普通短信与长短信在 PDU 模式下都能读到、不丢失, 长短信分片能按 UDH 重组.
不写数据库、不删除模块短信、不动服务配置. 结束时把模块切回文本模式.

用法:
    python3 pdu_test.py [--port /dev/ttyUSB3]

前置(在 N1 上):
    systemctl stop scc-lite        # 释放串口锁; 若提示 modem busy, 再停 scc-web

测试流程:
    1. 先跑一遍, 记下基线 (文本 x 条 / PDU y 条)
    2. 用手机给模块号码发一条普通短信, 再跑一遍: PDU 应多 1 条且解码正常
    3. 用手机发一条长短信 (中文>70字 或 英文>160字符), 再跑一遍:
       应看到同一 ref 的分片 (seq/total), 并打印合并后的完整内容
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modem import Modem  # noqa: E402


def main():
    port = "/dev/ttyUSB3"
    if "--port" in sys.argv:
        port = sys.argv[sys.argv.index("--port") + 1]

    m = Modem(port)
    m.open()
    try:
        txt = m.sms_list("all")
        print(f"[文本模式] {len(txt)} 条")
        for t in txt:
            print(f'  idx={t["index"]} {t["stat"]} {t["sender"]} '
                  f'{t["time"]} {t["body"][:30]}')

        msgs = m.sms_list_pdu(4)
        print(f"[PDU模式] {len(msgs)} 条")
        buf = {}  # (sender, ref) -> {total, parts, indices, time}
        n_fail = 0
        for p in msgs:
            d = p["decoded"]
            if not d:
                n_fail += 1
                print(f'  idx={p["index"]} 解析失败 '
                      f'raw={p["pdu"][:48]}...')
                continue
            if d["is_concat"]:
                frag = f' [分片 ref={d["ref"]} {d["seq"]}/{d["total"]}]'
                key = (d["sender"], d["ref"])
                b = buf.setdefault(
                    key, {"total": d["total"], "parts": {},
                          "indices": [], "time": d["time"]})
                if d["total"] > b["total"]:
                    b["total"] = d["total"]
                if d["seq"] not in b["parts"]:
                    b["parts"][d["seq"]] = d["body"]
                    b["indices"].append(p["index"])
            else:
                frag = ""
            print(f'  idx={p["index"]} {d["sender"]} {d["time"]}'
                  f'{frag} {d["body"][:30]}')

        print("[长短信重组]")
        if not buf:
            print("  无分片")
        for (sender, ref), b in buf.items():
            total, parts = b["total"], b["parts"]
            if (len(parts) == total
                    and all(s in parts for s in range(1, total + 1))):
                body = "".join(parts[s] for s in range(1, total + 1))
                print(f"  {sender} ref={ref}: {total}/{total} 收齐, "
                      f"合并 {len(body)} 字")
                print(f"    {body[:120]}")
            else:
                seqs = sorted(parts)
                print(f"  {sender} ref={ref}: {len(seqs)}/{total} 未收齐 "
                      f"(已有分片 {seqs})")

        print(f"汇总: 文本 {len(txt)} 条 / PDU {len(msgs)} 条 / "
              f"解析失败 {n_fail} 条")
        if len(msgs) < len(txt):
            print("!! PDU 少于文本, 有丢失嫌疑")
        else:
            print("OK PDU 不少于文本")
    finally:
        try:
            m._cmd("at+cmgf=1")  # 切回文本模式, 不影响服务重启
        except Exception:
            pass
        m.close()


if __name__ == "__main__":
    main()
