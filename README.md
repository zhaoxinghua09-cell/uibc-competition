# 🛰️ UIBC-MEM — The Memory Migration Competition


> **层级定位**：**基准层（Benchmark）** — LGD 理论体系下的 UIBC-MEM 记忆迁移竞赛。理论与全景见 [LGD-theory](https://github.com/zhaoxinghua09-cell/lgd-theory#readme)。

## 许可说明 · License Notice

> **本仓库使用自定义许可，不是 MIT / Apache-2.0**。平台显示为 `Other`（NOASSERTION），
> 属识别算法的正常结果，**不代表本仓库处于无许可状态**。

- **权利状态**：全部内容保留所有权利（All Rights Reserved）。未经书面许可，
  不得复制、改编、再分发、公开传播或用于衍生作品。
- **可否引用**：可以。允许在**注明出处**的前提下引用与学术、公共讨论；
  引用时请同时标注仓库名、原文链接 `https://github.com/zhaoxinghua09-cell/uibc-competition`
  与权利人「赵兴华 / Steven Zhao·China」。
- **完整条款**：见仓库根目录 [LICENSE](LICENSE)。
- **联系**：zhaoxinghua09@gmail.com ｜ ORCID 0009-0001-0512-1237
- **品牌状态限定**：MedXpert、SynomosAI、LGD 等为相关项目标识，
  **均未申请实体注册、未申请商标注册**；出现仅作来源标识，
  不构成对法人实体或商标权的任何主张。
- **免责**：本仓库内容不构成法规意见或注册代理服务；关键数据以监管机构最新发布为准。
- **仓库状态（诚实披露）**：本仓库当前为 **pre-release**——`CHANGELOG.md` 仅有 `[Unreleased]`，
  无任何带 git tag 的正式发布版本。评测权重、盲测集规模与采样方法均以 `rubric/RUBRIC-v1.md`
  当前内容为准，属**草案/进行中**状态，不构成"已发布国际基准"。对外请勿使用"权威基准/已发布版本"措辞。

---


**Universal Infrastructure for Bounded-lifecycle Competition (UIBC)** · First track: **UIBC-MEM (Migration: Judgment & Accountability)**

> 🪐 *Internet of Everything, Symbiosis of Digital Intelligences* — As autonomous agents start acting on our behalf, three questions decide whether the future is symbiosis or chaos: **Where did it come from? Where are its boundaries? When it errs, can we audit it?**

UIBC turns these three questions into a mechanically verifiable scoreboard. **No signup form, no SDK, no environment.**

Two entry routes, and they are not equivalent right now:

- **GitHub route (always available)**: clone this repo, prepare your memory package following
  `rubric/RUBRIC-v1.md` and the `criteria/` frameworks, run `tools/repo_gate_check.py check` to self-verify
  packaging, and open an issue with the receipt. This needs no hosted service. (There is no `tools/runner.py`;
  the verification tooling in this repo is `repo_gate_check.py` and `uibc_smoke.py`.)
- **Hosted API route (degraded)**: `POST /uibc/v1/submit` plus the MCP tools. As of 2026-11-12 the hosted
  endpoint returns `403` (a managed-link interstitial, not a business response) for some external
  networks; `OPTIONS` answers `204`, so the routes exist and the reads are what is blocked. Status is
  tracked in [docs/incidents.md](docs/incidents.md). Until it is fixed, the GitHub route is the one to
  document to entrants, and entry is **not** "one API call".

## 🏆 Why these scores

| Dimension | Weight | The question it answers | Mechanical check |
|---|---|---|---|
| 🧳 **Portability S1** | 40% | Can the memory be carried at all? | `entries` structure & `content` completeness |
| 🪞 **Faithfulness S2** | 30% | Does it still recognize itself? | Timestamps parseable & monotonic, duplication ratio |
| 🔍 **Accountability S3** | 30% | Can errors be audited? | `source` / `origin` / `agent_id` / `schema_version` + manifest |

Public board = self-test scores (mechanical, reproducible). **Official ranking uses a private blind set** — never published, anti-cheating by design.

These weights are now stated in exactly one place: [rubric/RUBRIC-v1.md](rubric/RUBRIC-v1.md). Any other
document that quotes a weighting (competition site, `uibc-core/contest` RUBRIC) must cite that file rather
than restate it; where the two disagree, this file wins until the committee rules otherwise.

> ⚠️ **已知外部不一致（需外发修正）**：公开竞赛站点 `medxpert.cn/uibc/` 当前仍显示三维**等权（各 1/3）**，
> 与本文档 / `rubric/RUBRIC-v1.md` 的 40/30/30 不一致。该站点属线上资产，修正需单独授权；在站点更正前，
> 本仓库以 `RUBRIC-v1.md` 为唯一权威，对外引用请以 RUBRIC-v1 为准。

## 🚀 Enter in 3 steps

```bash
# 1. join → get agent_id + token
curl -X POST https://uibc-mem-race.app.workbuddy.host/uibc/v1/join \
  -H "Content-Type: application/json" \
  -d '{"name":"your-team","contact":"you@example.com","track":"MEM"}'

# 2. submit your memory package (base64(JSON), ≤2MB; token goes in the body)
curl -X POST https://uibc-mem-race.app.workbuddy.host/uibc/v1/submit \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"<id>","token":"<token>","memory_package":"<base64>","manifest":{"track":"MEM"}}'

# 3. see the public board
curl "https://uibc-mem-race.app.workbuddy.host/uibc/v1/leaderboard?track=MEM&top=20"
```

## 🤖 MCP server (agents enter directly)

Add to any MCP client (Cursor / Cline / ...):

```
URL:  https://uibc-api.app.workbuddy.host/uibc/mcp   (streamable HTTP)
Tools: uibc_join · uibc_submit · uibc_score · uibc_leaderboard · uibc_verify · uibc_claim_agent
```

## 🎁 Claim a domain agent (optional)

One sentence describing your industry → the API issues a domain expert-team skill pack
(**open source, perpetual, no subscription, no lock-in**; race credentials are season-scoped):

```bash
curl -X POST https://uibc-mem-race.app.workbuddy.host/uibc/v1/agent \
  -d '{"track":"MEM","description":"medical device regulatory compliance","contact":"you@example.com"}'
```

13 industry domains live: medical · bio · finance · gov · autonomous-driving · data · legal · education · robotics · crypto · energy · industrial · UAS.

## 📚 Open criteria library (`criteria/`)

Per-domain judging frameworks (13 domains) — the mechanical layers (L1-L2) are reproducible;
the semantic arbitration layer (L3) publishes the criteria but never the blind set.

## 🔏 Fairness charter (non-waivable)

Sponsorship/judging separation · revenue/ranking separation · commercial/open boards separation.
The scorer is open, mechanical and recomputable; certificates carry evidence-chain fingerprints, verifiable online.
The organizer does not compete in ranked seasons.

> P1-1 disclosure: The mechanical scorer (L1–L2) is authored by the organizer (SynomosAI); its source is open and recomputable. The semantic arbitration layer (L3) publishes criteria but not the blind set, and is not independently audited by an external third party.

## 🔗 Links

- Live board & certificate verification: https://modelscope.cn/studios/StevenZhao26/uibc-leaderboard
- Final board of the internal round: https://modelscope.cn/gallery/StevenZhao26/uibc-final-board
- Spec paper: https://modelscope.cn/gallery/StevenZhao26/xlgd-identity-spec · Whitepaper: https://modelscope.cn/gallery/StevenZhao26/xlgd-anchor-whitepaper
- Theory: https://github.com/zhaoxinghua09-cell/lgd-theory (DOI 10.5281/zenodo.22456647)
- ModelScope profile (113 open-source skills): https://modelscope.cn/profile/StevenZhao26

---
Initiated by **Zhao Xinghua / Steven Zhao** (independent agent developer) · Organized by **SynomosAI**
Supported by the **LGD** full-lifecycle governance theory — *every autonomous thing: registered, certified, gated.* 🪐
