# Config Hash Identities

“默认 Config hash”不是一个可审计的唯一概念。任何报告、测试或
manifest 说明都必须写出具体 hash 字段名、schema、输入 fixture 和
base directory；不得只写模糊的“Config hash”。

## 2026-07-15 sealing cross-version check

使用同一固定 fixture 分别执行 `phase1.1-complete-v1` 与 Phase 1.2A
sealing commit 的 `nmsim.config_contract`：

```text
Config()
base_dir=/Users/aldrich/Desktop/agent模拟市场
execution_context={}
```

Phase 1.1 代码通过 `git archive phase1.1-complete-v1` 展开到临时目录；
未切换或修改历史 ref，也未修改历史运行。两版结果逐字段相同：

| Identity | Phase 1.1 complete | Phase 1.2A sealing check |
|---|---|---|
| `config_hash_schema_version` | `1.0` | `1.0` |
| `config_classification_hash` | `299747fa4527f820ffbc9fbd13186ab26887f8f6cca4c24b0e36d1edcf7dbebd` | same |
| `full_effective_config_hash` | `1a36131b7dec0a90af315c03b1bdb748d7b90c5d6defa3cbd2b31db437af69dd` | same |
| `scientific_config_hash` | `891609d7ff29b8579fc51dd011c1ebcda9f2f8d8ef71c304a83211089fcc1b12` | same |
| `model_request_config_hash` | `161ee24c72dcf446453c588654aba1e7694c0137b3bacd5eb8f6f071e869b960` | same |
| `execution_config_hash` | `1a7038f9d4253429565c25f13118e237a71d6c50a0e128979551cf32bc4f742c` | same |

所有 effective/scientific/model-request/execution 规范化 summary 的字段和值也
完全一致，没有跨版本字段级差异。

## 两个曾被简称为“默认 Config hash”的值

`f0508c233c669749eedc1eabc93d2bea97d2438d8a1fd8b51923ff9195697c07`
是冻结测试中的 legacy raw-default identity：

```python
sha256(json.dumps(
    asdict(Config()),
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8"))
```

它没有 hash schema envelope，不使用 tagged float/path/endpoint/credential
规范化，也不区分 scientific、model-request 和 execution；它只能说明
dataclass 默认值的 raw JSON 投影。两版对此值也相同。

`1a36131b…a69dd` 则是 `config_hash_schema_version=1.0` 的
`full_effective_config_hash`：对全部 38 个有效字段的 secret-free 规范化
summary 加上 `identity=full_effective_config` 和 schema envelope 后计算。它包含
execution 字段，所以本例明确绑定上述 `base_dir`；它不能代替 strict replay
分别使用的 scientific/model-request identity。

两者输入投影和算法不同，不是互相矛盾的计算结果，也不得直接比较。

## 固定 fixture 的规范化分类摘要

Scientific（27 fields）：

```text
broadcast_mode=all; demote_influencer=false; digest_size=4;
fundamental_value=float.hex(100.0); initial_price=float.hex(100.0);
kappa=float.hex(0.12); leverage_enabled=false;
leverage_fraction=float.hex(0.5); leverage_ratio=float.hex(2.5);
leverage_spread=float.hex(0.5); maintenance_margin=float.hex(0.25);
max_llm_agents=40; n_llm_agents=6; n_neighbors=2; n_noise_agents=8;
n_rounds=24; news_round=12; news_text=<default breaking-news text>;
population={mode:legacy,counts:null,effective_cast:null}; recent_window=5;
reference_path={configured:false,kind:null,size_bytes:null,sha256:null}; seed=7;
seed_fraction=float.hex(0.34); social_enabled=true; social_mode=network;
social_weight=float.hex(1.0); topology=scale_free
```

Model request（9 fields）：

```text
provider=auto; model=""; cheap_model=""; use_cheap_model=false;
openai_model=MiniMax-M2.7; temperature=float.hex(0.0); max_tokens=1024;
cache_enabled=true;
openai_base_url={configured:true,
  endpoint_identity_sha256:66e21f44b31bae951b37de32684b004d81c0821d956eb3351432770f11aad0c1,
  userinfo_redacted:false}
```

Execution（2 Config fields plus empty runtime context）：

```text
openai_api_key={configured:false,value:<not-configured>};
out_dir={kind:path_identity,
  resolved_path_sha256:61d1d081832e098bca6f1a976f83a0c4a16b822fc20a088e23bda70ac5faddc0};
runtime={}
```

秘密没有进入任何 summary。改变 execution path/base_dir 可以改变 full/execution
hash，但不得改变 scientific/model-request hash；改变科学输入文件字节则必须改变
scientific hash。
