# 项目质量门（阶段检查清单）

严重级别：error（硬约束/身份/人数/逐字/引用/时长/权利不成立 → 不得进入下一阶段）/
warning（风险/缺次要信息 → 人工确认后可继续）/ info（改进建议）。
平均分不能抵消 hard gate。

## Brief 门
受众/用途/总时长/画幅/真实交付规格明确；创意硬约束可检查；参考权利边界明确；
生成 provider/次数/预算未授权时保持 false；发布授权独立记录。

## Story 门
世界规则和角色目标不矛盾；每场戏有进入状态/变化/结束状态；视觉/颜色/声音母题可执行；
禁止变化与剧情变化分开。

## Asset 门
每个主要角色/道具/场景有稳定 ID；identity invariants 与 state variables 分开；
关键多视图存在或缺失已登记；每个参考有 inherit/exclude、权利状态和批准状态；
进入镜头的状态版本 approved。

## Scene/Spatial 门
场次顺序/地点/时间/故事目标明确；区域/出入口/固定锚点/唯一物体可定位；
允许资产和光源有单一数据源；场次 open/close state 可连接。

## Shot Contract 门
一个主叙事功能；精确人数和允许集合；open_state/beat_timeline/close_state 完整；
camera_start/path/end 只有一个主运动；动作与逐字对白服从时长；
continuity_in/out、must_hold、changes_here、must_not_appear 一致；audio cues 与 risk focus 明确。

## Prompt 门
原始意图与硬约束快照存在；模型无关主提示词与平台适配层分离；本地审计无 error；
负向约束来自当前风险并已去重；prompt ID/version/parent/changed_variables/hash 可追溯。

## 生成前门
镜头状态至少 prompt_ready；资产和 prompt approved；参考权利可用于当前用途；
当前任务明确 provider/调用次数/预算和生成授权；不存在未解决 error。
若用户只要求搭建/写提示词/审计，此门不自动打开。

## 生成记录门
每次调用有 generation ID/shot ID/prompt ID/batch ID；provider/model/参数/时间/状态/路径/成本记录；
失败码具体，不只写"效果不好"；批次内变量固定；迭代含 hypothesis/changed_variables/decision/next_action。

## 选片门
候选按叙事/身份/空间/动作/摄影/视听/连续性评审；select 有唯一 selection 记录；
已知缺陷和连续性影响明确；选择不是只因"最漂亮"；error 已修复或有批准 waiver。

## Edit 门
镜头顺序/切点/动作接点成立；环境底床/对白/拟音/音乐/字幕边界成立；
调色不掩盖身份/人数/连续性硬错；后期修复可追溯到 shot/generation；占位和未解决问题明确。

## Delivery 门
成片时长/帧率/画幅/分辨率/音频/字幕符合 brief；所有镜头有最终选择或批准豁免；
源文件/生成记录/提示词/版本可追溯；字体/声音/参考/第三方材料权利已确认；
片尾署名和许可声明按项目要求完成；发布目标与权限单独确认。

ready_for_review / ready_for_delivery / published 不能互相替代。
自动质检检查结构/表头/ID/引用/状态/显式数值规则，不观看媒体，也不判断表演美感、
叙事效果、真实口型或权利事实真伪；最终人工必须观看画面、听声音、核对权利与交付。
