# 测试说明

## 运行

```bash
pip install -r requirements.txt
pytest -q
# 无 pytest 时：python -m unittest discover -s tests -t .
```

测试不访问任何外部网络：扶摇、iFinD、LLM 都用伪造的 HTTP 会话模拟；"当前时间"固定为 2026-10-14 12:00（+08:00）。
当前共 75 个测试，全部通过。

## 覆盖矩阵

### 1. 主链路：识别、合并、跟踪同一事件的不同来源与版本

| 场景 | 预期 | 测试 |
|---|---|---|
| 尚无任何来源 | 所有事实为「暂无信息」 | `test_before_any_source_everything_missing` |
| 只有传闻 | 「未证实」，阶段为"传闻" | `test_rumor_stage` |
| 媒体报道出现 | 覆盖传闻，状态「媒体报道·待证实」 | `test_media_overrides_rumor` |
| 两家同等级媒体对价不同 | 「存在冲突」，列出双方证据 | `test_same_rank_media_conflict` |
| 公司回应 | 冲突消解为公司口径 | `test_company_reply_resolves_conflict` |
| 公司否认停牌 | 停牌传闻变为「已否认」 | `test_denial_changes_conclusion` |
| 正式公告 | 确认对价、进展、停牌；替代公司此前口径 | `test_announcement_confirms_and_supersedes_company_reply` |
| 更正公告 | 股权比例 51% → 55% | `test_correction_changes_stake` |
| 原文更新版 | 旧版本主张作废；生效前看不到新版本 | `test_source_update_supersedes_old_version`、`test_only_effective_sources_are_used` |
| 转载 | 计为 1 个独立来源；走样转述被标记且不参与判定 | `test_repost_counts_as_one_independent_source`、`test_distorted_repost_is_flagged_and_not_deciding` |
| 溯源 | 最早出处指向股吧传闻，转载链正确 | `test_origin_points_to_earliest_rumor` |
| 导入新公告 | 自动归入已有事件，结论变化正确 | `test_announcement_is_merged_and_changes_conclusion` |
| 导入无关传闻 | 新建事件并标注待确认 | `test_unrelated_rumor_creates_new_event` |
| 导入更新版本 | 继承原事件与原披露时间，更新时间单独记录 | `test_version_update_inherits_event_and_publish_time` |
| 时间回放 | 回放到导入前，结论不受新来源影响 | `test_replay_before_ingested_source_is_unchanged` |

### 2. 事实 / 观点 / 推测 / 传闻与证据权重

| 场景 | 测试 |
|---|---|
| 观点和推测不进入事实结论（研报 EPS 预测、"或上调至 26 亿"） | `test_opinions_and_speculation_kept_out_of_facts` |
| 传闻权重按 7 天半衰期衰减，事实不衰减 | `test_rumor_weight_decays_fact_does_not` |
| 规则抽取识别传闻词、推测词 | `test_rumor_words_mark_rumor`、`test_speculation_words` |

### 3. 四类时间与"更新 / 否认 / 更正 / 过期改变结论"

| 场景 | 测试 |
|---|---|
| 传闻 14 天无人证实 → 已过期；公告给出真实值 → 已确认 | `test_rumor_expires_after_ttl` |
| 关注函回复期限已过 → 逾期待核实；回复后提示消失 | `test_overdue_flag_then_cleared` |
| 过期由"时间推移"触发并记录在结论变化中 | `test_changes_include_time_triggered_expiry` |
| 进展类事实以最新为准，不误判冲突 | `test_progress_slot_does_not_conflict` |
| 同一发布方以最新说法为准 | `test_same_issuer_latest_wins_over_all_earlier` |
| 撤回 | `test_retraction_removes_claim` |
| 抓取延迟按生效时间计算 | `test_fetch_lag_warning_uses_effective_time` |
| 盘后披露顺延、周五晚顺延到周一、盘中当日 | `TradeDate` 测试组 |
| 请求"未来时刻"的结论被截断为当前时间 | `test_future_as_of_is_clamped_to_now` |

### 4. 数据缺失 / 接口失败

| 场景 | 预期 | 测试 |
|---|---|---|
| 未配置扶摇 Key | 明确显示「未配置」，不返回假数据 | `test_not_configured`、`test_watchlist_without_fuyao_key_is_explicit` |
| 扶摇错误码 2001/2003/3001/3002/4001/5002 | 分别映射为调用失败或数据缺失，附 request_id | `test_error_codes` |
| HTTP 429、超时、非 JSON | 显示具体原因 | `test_http_429_timeout_and_non_json`、`test_fuyao_timeout_surfaces_in_watchlist` |
| 接口成功但返回空列表 | 标为「数据缺失」而非正常 | `test_empty_items_is_missing_not_ok` |
| 鉴权失败时请求行情 | 行情区显示错误，不画图 | `test_fuyao_auth_error_surfaces_in_market` |
| 构造标的请求行情 | 显示"无真实行情"，不造数据 | `test_market_for_constructed_ticker_has_no_fake_data` |
| 交易日历不可用 / 超出覆盖范围 | 按工作日估算并标注 | `test_fallback_is_labelled`、`test_out_of_calendar_range_is_labelled`、`test_trade_date_fallback_is_labelled_when_calendar_missing` |
| LLM 未配置 / 超时 / 不支持 response_format | 降级为规则抽取并记录原因；400 时去掉 response_format 重试 | `LLMPath` 测试组 |
| iFinD 未配置 / 未配置工具名 / HTTP 401 | 明确提示原因 | `test_ifind_not_configured`、`test_search_without_tool_name`、`test_http_error_is_reported` |
| iFinD SSE 响应解析、会话 ID 透传 | 正确解析新闻列表 | `test_search_parses_sse_result` |

### 5. 极端输入与合规边界

| 场景 | 预期 | 测试 |
|---|---|---|
| 原文为空、超长（>5000 字）、来源类型非法、时间格式错误、披露时间在未来、引用不存在的版本或事件 | 拒绝并给出中文原因 | `test_invalid_inputs` |
| LLM 编造原文中没有的引用 | 该主张被拒绝，不进入结论 | `test_rejects_fabricated_quote`、`test_llm_output_is_parsed_and_validated` |
| 未知槽位、非法枚举、缺取值、数值不可解析、日期格式错误 | 拒绝并记录原因 | `test_rejects_unknown_slot_bad_enum_missing_value` |
| "明天会涨吗""能不能买入""目标价""要不要加仓""还能涨多少" | 拒答，引导查看事实与出处 | `test_prediction_and_advice_refused` |
| 事实问题 | 只用当前结论回答，附发布方与时间 | `test_fact_question_answered_with_citations` |
| 回放到冲突时刻提问 | 回答中如实说明存在冲突 | `test_answer_respects_replay_time_and_conflict` |
| 问到尚无来源的事实 | 回答"没有任何来源提及"，不编造 | `test_missing_fact_is_not_invented` |

## 手动验收路径（约 3 分钟）

1. 首页：确认关注列表中两只构造标的分别显示"直接当事方 / 间接关联"，行情显示"构造标的，无真实行情"。
2. 进入事件，点「◀ 上一条证据」逐步回放，核对 README"一分钟看懂"表格中的每一步。
3. 回放到 09-02 13:00：事实账本「交易对价」为冲突；点开该行，看到 4 条说法及各自状态、权重、原文；冲突面板显示双方证据。
4. 点时间线中"某财经自媒体"，抽屉中看到原文高亮与"转述走样"提示。
5. 问一问输入"辰星电子还能涨吗"，应拒答；输入"停牌了吗"，应回答"不停牌"并附出处。
6. 导入页点「示例：签署正式协议公告」→ 导入，结果显示归入原事件、对价变为 24.5 亿、进展变为已签正式协议；回到事件页确认。
7. 导入「示例：新传闻（另一事件）」，应新建事件并标注待确认。
8. 配置扶摇 Key 后，在首页添加一只真实股票（如 600519.SH），确认显示扶摇快照、数据时间和来源；顶部状态显示扶摇正常。

## 未覆盖 / 需真实凭证验证

- 扶摇、iFinD、LLM 的真实调用（需要 Key）。配置后建议按"手动验收路径"第 8 步和导入流程各跑一遍，并把发现记录到 `AI_USAGE.md`。
- 浏览器兼容性只在 Chromium 中检查过。
