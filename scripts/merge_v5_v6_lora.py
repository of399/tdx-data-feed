"""LoRA Adapter Fusion: 合并 v5 + v6 adapters → v8
- 不重新训练，直接权重平均
- 数学基础：LoRA = base + A·B，加权平均 (α₁·W₁ + α₂·W₂)
- 输出：v8-merged → 可直接 merged + GGUF + ollama
"""
import shutil
from pathlib import Path

from safetensors.torch import load_file as st_load, save_file as st_save

V5_ADAPTER = Path('/home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v5-simple/checkpoint-1188')
V6_ADAPTER = Path('/home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v6-gpu1/checkpoint-984')
V8_ADAPTER = Path('/home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v8-merged')

W_V5 = 0.5
W_V6 = 0.5


def main():
    V8_ADAPTER.mkdir(parents=True, exist_ok=True)
    print('=== LoRA Adapter Fusion: v5 + v6 → v8 ===')
    print(f'  v5: {V5_ADAPTER} (w={W_V5})')
    print(f'  v6: {V6_ADAPTER} (w={W_V6})')

    v5_st = list(V5_ADAPTER.glob('adapter_model.safetensors'))
    v6_st = list(V6_ADAPTER.glob('adapter_model.safetensors'))
    if not v5_st or not v6_st:
        print('❌ 找不到 adapter_model.safetensors')
        return

    print(f'\n加载 v5 weights: {v5_st[0].name}')
    v5_w = st_load(str(v5_st[0]))

    print(f'加载 v6 weights: {v6_st[0].name}')
    v6_w = st_load(str(v6_st[0]))

    common = set(v5_w.keys()) & set(v6_w.keys())
    only_v5 = set(v5_w.keys()) - common
    only_v6 = set(v6_w.keys()) - common
    print(f'\n  v5 tensors: {len(v5_w)}')
    print(f'  v6 tensors: {len(v6_w)}')
    print(f'  共有: {len(common)}')
    print(f'  仅 v5: {len(only_v5)}')
    print(f'  仅 v6: {len(only_v6)}')

    merged = {}
    for k in common:
        if v5_w[k].dtype.is_floating_point:
            merged[k] = (W_V5 * v5_w[k].float() + W_V6 * v6_w[k].float()).to(v5_w[k].dtype)
        else:
            merged[k] = v5_w[k]

    for k in only_v5:
        merged[k] = v5_w[k]
    for k in only_v6:
        merged[k] = v6_w[k]

    out_path = V8_ADAPTER / 'adapter_model.safetensors'
    st_save(merged, str(out_path), metadata={'format': 'pt'})
    print(f'\n✓ 合并权重保存: {out_path}')

    for fname in ['adapter_config.json', 'tokenizer.json', 'tokenizer_config.json',
                  'special_tokens_map.json', 'vocab.json', 'merges.txt',
                  'added_tokens.json']:
        for src in (V5_ADAPTER, V6_ADAPTER):
            f = src / fname
            if f.exists():
                shutil.copy(f, V8_ADAPTER / fname)
                break

    readme = V8_ADAPTER / 'README.md'
    readme.write_text(f"""# v8 LoRA (v5+v6 fusion)

## 生成方式
直接权重平均（非重新训练）：
- W_v8 = {W_V5} × W_v5 + {W_V6} × W_v6
- v5: {V5_ADAPTER}
- v6: {V6_ADAPTER}

## 数据来源
- v5: tdx_v5_train (12656 条)
- v6: tdx_v6_train (5238 条精选)
- 合计: 17894 条

## 使用
```python
from peft import PeftModel
from transformers import AutoModelForCausalLM

base = AutoModelForCausalLM.from_pretrained("/home/jiuben/models/Qwen3-14B")
model = PeftModel.from_pretrained(base, "{V8_ADAPTER}")
merged = model.merge_and_unload()
```
""")
    print(f'✓ README: {readme}')

    print('\n=== 完成 ===')
    print(f'  v8 adapter: {V8_ADAPTER}')


if __name__ == '__main__':
    main()
