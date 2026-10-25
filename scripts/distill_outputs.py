import concurrent.futures as cf
import json
import os
import re
import sys
import time
import urllib.request

BASE = os.environ.get("TDX_OLLAMA_URL", "http://127.0.0.1:11434")
# 教师模型必须显式指定。这里曾写死一个 tag——本机现在没有它，而仓库里也没有任何生产者
# （无 Modelfile、无 ollama create 步骤）；旧名字和它是怎么静默产出空数据集的，
# 记在 tests/test_distill_teacher_model.py 的模块文档里。
MODEL = os.environ.get("TDX_DISTILL_MODEL", "")
NUM_PREDICT = int(os.environ.get("NUM_PREDICT", "100"))

# 思考链标签一律按字符拼装成正则，源码里不写字面控制标记：这些标记在部分渲染/传输链路里
# 会被整段吞掉，pattern 写错成「从来没出现过的那对标签」时，从源码里也看不出问题。
_LT, _GT, _SLASH = chr(60), chr(62), chr(47)
_THINK_OPEN = _LT + r'\|?think\|?>'
_THINK_CLOSE = _LT + r'\|?' + _SLASH + r'think\|?>'
_IM_END = _LT + r'\|im_end\|?>'
# 完整思考块：起始标签到（竖线闭合标签 或 im_end）整体丢掉，连带尾随空白。
_THINK_BLOCK = re.compile(_THINK_OPEN + '.*?(?:' + _THINK_CLOSE + '|' + _IM_END + ')' + r'\s*', re.S)
# 被截断的块只剩单边标签时，单独抹掉标签本体——历史数据集里 1,728 条就是这个形状。
_STRAY_OPEN = re.compile(_THINK_OPEN)
_STRAY_CLOSE = re.compile(_THINK_CLOSE + '|' + _IM_END)


def clean(text):
    text = _THINK_BLOCK.sub('', text)
    text = _STRAY_OPEN.sub('', text)
    text = _STRAY_CLOSE.sub('', text)
    return text.strip()


class DistillError(RuntimeError):
    """教师不可用或答了个空。不许退化成 ""——那会被当成一条正常样本写进语料。"""


def call(prompt):
    payload = {"model": MODEL, "prompt": prompt, "stream": False,
               "options": {"num_predict": NUM_PREDICT, "temperature": 0.0}}
    req = json.dumps(payload).encode()
    last = None
    for _ in range(3):
        try:
            r = urllib.request.urlopen(BASE + "/api/generate", data=req, timeout=180)
            text = clean(json.loads(r.read()).get("response", ""))
        except Exception as e:
            last = e
            time.sleep(2)
            continue
        if not text:
            raise DistillError(
                f"教师 {MODEL} 从 {BASE} 返回空答案（num_predict={NUM_PREDICT}）："
                "拒绝把空 output 写进训练集")
        return text
    raise DistillError(f"教师 {MODEL} 连续 3 次调用 {BASE}/api/generate 均失败：{last!r}")

def main():
    if not MODEL:
        sys.exit("TDX_DISTILL_MODEL 未设置：教师模型必须显式指定。"
                 "本机可选 tag 见 `make deploy-list`，"
                 "例如 make distill MODEL=qwen3-14b-tdx-v4fwd")
    SK = sys.argv[1]
    OUT = sys.argv[2] if len(sys.argv) > 2 else "/home/jiuben/tdx-data-feed/data/distill/full.jsonl"
    LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    CONC = int(sys.argv[4]) if len(sys.argv) > 4 else 8
    rows = [json.loads(l) for l in open(SK, encoding='utf-8')]
    if LIMIT:
        rows = rows[:LIMIT]
    done = 0
    if os.path.exists(OUT):
        with open(OUT, encoding='utf-8') as f:
            for _ in f:
                done += 1
    print(f"total={len(rows)} done={done} conc={CONC} num_predict={NUM_PREDICT}")
    rows = rows[done:]
    fout = open(OUT, 'a', encoding='utf-8')
    prompts = [r['instruction'] + "\n" + r['input'] for r in rows]
    t0 = time.time()
    ex = cf.ThreadPoolExecutor(max_workers=CONC)
    written = 0
    try:
        for i, out in enumerate(ex.map(call, prompts, chunksize=4)):
            s = rows[i]
            s['output'] = out
            fout.write(json.dumps(s, ensure_ascii=False) + '\n')
            written += 1
            if (i + 1) % 200 == 0:
                el = time.time() - t0
                print(f"  {done+i+1}  {el:.0f}s  {(i+1)/el:.2f}/s")
                fout.flush()
    except DistillError as e:
        # 排队中的 prompt 全部取消：教师已经死了，让剩下几千条各重试三遍只是空转。
        # 已写出的行留在原地——续跑按行数跳过，不会重复也不会漏。
        ex.shutdown(wait=False, cancel_futures=False)
        fout.close()
        sys.exit(f"DISTILL_FAILED 本次已写 {written} 行后中止：{e}")
    ex.shutdown(wait=True)
    fout.close()
    print("DISTILL_DONE", OUT)

if __name__ == "__main__":
    main()
