import argparse;
import random;
import statistics;
from dataclasses import dataclass, field, replace;
from pathlib import Path;

import matplotlib;

matplotlib.use("Agg");

import matplotlib.pyplot as plt;


# 常量

V_随机种子 = 20261126;  # 固定种子，方便复现同一批任务和报价。
V_任务总轮次 = 24;  # 跑 24 轮，每轮相当于一次调度窗口。
V_每轮任务数区间 = (1, 3);  # 每轮新到 1 到 3 个任务。
V_任务能力池 = ("数据分析", "文本生成", "图像识别", "代码修复", "数据清洗");  # 任务可能出现的能力需求。
V_工作量区间 = (2.0, 9.0);  # 单个任务的工作量，单位是算力小时。
V_预算系数区间 = (3.2, 6.5);  # 预算上限按工作量乘这个系数给。
V_最低质量区间 = (0.55, 0.75);  # 任务对交付质量的最低要求。
V_价值上浮区间 = (1.1, 1.5);  # 任务真实价值比预算高一截，用来算平台效用。
V_负载惩罚系数 = 0.85;  # 负载平方计入成本，忙的 Agent 报价自然变贵。
V_时延单价 = 0.35;  # 每秒响应时间折算多少钱，慢的要吃亏。
V_质量溢价系数 = 0.35;  # 质量高于 0.5 的 Agent 可以加点价。
V_报价抖动幅度 = 0.06;  # 模拟行情波动，报价上下浮动 6%。
V_低于成本容忍度 = 0.85;  # 报价低于真实成本的 85%，按恶意低价处理。
V_信誉红线 = 0.55;  # 低于这个分数的 Agent 停牌。
V_信誉恢复速度 = 0.01;  # 停牌期间每轮回一点信誉，够线才能重新接单。
V_恶意报价扣分 = 0.02;  # 每拦一次恶意报价扣的信誉。
V_返工扣款比例 = 0.4;  # 质量不达标只结六成，剩下算返工代价。
V_返工信用扣分 = 0.05;  # 返工一次扣 0.05 信誉。
V_成功信用加分 = 0.02;  # 正常交付一次加 0.02 信誉。
V_最大返工次数 = 2;  # 同一任务最多重拍两次，避免无限流拍。
V_负载衰减比例 = 0.55;  # 每轮释放 55% 的历史负载，模拟任务被消化。
V_备用Agent加价率 = 1.6;  # 无人竞标时备用 Agent 按成本 1.6 倍结算。
V_备用Agent负载上限 = 0.9;  # 备用 Agent 负载超过 0.9 就转人工。
V_综合评分权重 = {"价格": 0.40, "质量": 0.25, "时延": 0.15, "信誉": 0.20};  # 价格、质量、时延、信誉四项权重，加起来是 1。
V_图表配色 = ("#D98BA3", "#9CB0C4", "#C9B8AC", "#8FA98F", "#A88FA8", "#B08B8B");  # 低饱和配色，出图不刺眼。
V_输出目录 = Path(__file__).resolve().parent / "output";  # 图表输出目录，运行后自动建。


# 数据结构

@dataclass
class Task:
	# 待拍任务：预算是保留价，价值用来算平台效用，attempts 记返工次数。
	task_id: str;
	capability: str;
	workload: float;
	budget: float;
	min_quality: float;
	value: float;
	arrive_round: int;
	attempts: int = 0;


@dataclass
class Agent:
	# 竞标方：技能分决定交付质量上限，单位成本决定报价下限。
	agent_id: str;
	skills: dict;
	unit_cost: float;
	base_latency_ms: float;
	quality: float;
	capacity: float;
	reputation: float;
	bid_probability: float = 1.0;
	game_probability: float = 0.0;
	load: float = 0.0;
	is_backup: bool = False;
	win_count: int = 0;
	revenue: float = 0.0;
	cost: float = 0.0;


@dataclass
class Bid:
	# 一条报价，valid 为 False 时 note 里写驳回原因。
	agent_id: str;
	price: float;
	latency_ms: float;
	quality: float;
	reputation: float;
	valid: bool = True;
	note: str = "";
	score: float = 0.0;


@dataclass
class RoundRecord:
	# 一轮拍卖的结果，status 是完成、返工、兜底或人工接管。
	round_index: int;
	task_id: str;
	capability: str;
	value: float;
	winner: str;
	payment: float;
	cost: float;
	quality: float;
	bid_count: int;
	valid_count: int;
	blocked_count: int;
	status: str;
	note: str = "";


@dataclass
class RunReport:
	# 一次完整模拟的产物：逐轮记录、负载曲线、Agent 终态和汇总指标。
	mechanism: str;
	records: list;
	load_history: list;
	agents: list;
	metrics: dict = field(default_factory=dict);


# 核心逻辑

def 生成任务列表(rng, rounds):
	# 一次造出整场任务，三种机制看到的是同一批任务，对比才公平。
	# rng 是随机数源，rounds 决定任务铺到第几轮；返回任务列表。
	tasks = [];
	for round_index in range(1, rounds + 1):
		for _ in range(rng.randint(*V_每轮任务数区间)):
			workload = round(rng.uniform(*V_工作量区间), 2);
			capability = rng.choice(V_任务能力池);
			budget = round(workload * rng.uniform(*V_预算系数区间), 2);
			tasks.append(Task(
				task_id=f"T{len(tasks) + 1:03d}",
				capability=capability,
				workload=workload,
				budget=budget,
				min_quality=round(rng.uniform(*V_最低质量区间), 2),
				value=round(budget * rng.uniform(*V_价值上浮区间), 2),
				arrive_round=round_index,
			));
	return tasks;


def 构建异构Agent():
	# 四个竞标 Agent 加一个备用，成本、时延和技能刻意拉开差距。
	# 返回 Agent 列表，每场模拟都重新造一份，状态互不影响。
	return [
		Agent(
			agent_id="agent_A",
			skills={"数据分析": 0.97, "文本生成": 0.90, "图像识别": 0.50, "代码修复": 0.72, "数据清洗": 0.86},
			unit_cost=2.50,
			base_latency_ms=1200,
			quality=0.95,
			capacity=30.0,
			reputation=0.93,
			load=0.15,
			game_probability=0.05,
		),
		Agent(
			agent_id="agent_B",
			skills={"数据分析": 0.86, "文本生成": 0.88, "图像识别": 0.60, "代码修复": 0.85, "数据清洗": 0.90},
			unit_cost=1.50,
			base_latency_ms=2600,
			quality=0.86,
			capacity=18.0,
			reputation=0.88,
			load=0.45,
			game_probability=0.08,
		),
		Agent(
			agent_id="agent_C",
			skills={"数据分析": 0.72, "文本生成": 0.70, "图像识别": 0.55, "代码修复": 0.60, "数据清洗": 0.95},
			unit_cost=0.90,
			base_latency_ms=5200,
			quality=0.82,
			capacity=10.0,
			reputation=0.72,
			load=0.80,
			game_probability=0.30,
		),
		Agent(
			agent_id="agent_D",
			skills={"数据分析": 0.70, "文本生成": 0.60, "图像识别": 0.97, "代码修复": 0.65, "数据清洗": 0.60},
			unit_cost=2.00,
			base_latency_ms=1800,
			quality=0.90,
			capacity=14.0,
			reputation=0.90,
			load=0.25,
			bid_probability=0.95,
		),
		Agent(
			agent_id="agent_backup",
			skills={"数据分析": 0.90, "文本生成": 0.88, "图像识别": 0.88, "代码修复": 0.88, "数据清洗": 0.90},
			unit_cost=3.20,
			base_latency_ms=8000,
			quality=0.92,
			capacity=50.0,
			reputation=1.00,
			is_backup=True,
		),
	];


def 复制任务列表(tasks):
	# 复制一份任务并清掉返工次数，别让上一场模拟的痕迹带进下一场。
	# tasks 是原始列表；返回新列表。
	return [replace(task, attempts=0) for task in tasks];


def 计算报价(agent, task, rng):
	# 基础成本、负载拥堵、时延折算、质量溢价叠在一起，再抖一下模拟行情。
	# agent 是投标方，task 是待拍任务，rng 提供随机数。
	# 返回 Bid；不合格的报价 valid 为 False，note 里写清原因。
	if task.capability not in agent.skills:
		return Bid(
			agent_id=agent.agent_id,
			price=0.0,
			latency_ms=0.0,
			quality=0.0,
			reputation=agent.reputation,
			valid=False,
			note="能力不匹配",
		);
	if agent.reputation < V_信誉红线:
		return Bid(
			agent_id=agent.agent_id,
			price=0.0,
			latency_ms=0.0,
			quality=0.0,
			reputation=agent.reputation,
			valid=False,
			note="信誉低于红线，暂停接单",
		);
	if rng.random() > agent.bid_probability:
		return Bid(
			agent_id=agent.agent_id,
			price=0.0,
			latency_ms=0.0,
			quality=0.0,
			reputation=agent.reputation,
			valid=False,
			note="超时未响应",
		);
	skill = agent.skills[task.capability];
	base_cost = task.workload * agent.unit_cost;
	latency_ms = agent.base_latency_ms * (1.0 + agent.load);
	load_cost = base_cost * agent.load ** 2 * V_负载惩罚系数;
	latency_cost = latency_ms / 1000.0 * V_时延单价 * task.workload;
	quality_cost = base_cost * (agent.quality - 0.5) * V_质量溢价系数;
	price = base_cost + load_cost + latency_cost + quality_cost;
	price = price * (1.0 + rng.uniform(-V_报价抖动幅度, V_报价抖动幅度));
	price = round(max(price, base_cost), 2);
	quality_claim = round(min(0.99, skill * agent.quality * (1.0 - 0.35 * agent.load)), 3);
	if rng.random() < agent.game_probability:
		price = round(base_cost * 0.6, 2);
	if price > task.budget:
		return Bid(
			agent_id=agent.agent_id,
			price=price,
			latency_ms=latency_ms,
			quality=quality_claim,
			reputation=agent.reputation,
			valid=False,
			note="报价超出预算上限",
		);
	if price < base_cost * V_低于成本容忍度:
		return Bid(
			agent_id=agent.agent_id,
			price=price,
			latency_ms=latency_ms,
			quality=quality_claim,
			reputation=agent.reputation,
			valid=False,
			note="异常低价，疑似恶意抢单",
		);
	if quality_claim < task.min_quality:
		return Bid(
			agent_id=agent.agent_id,
			price=price,
			latency_ms=latency_ms,
			quality=quality_claim,
			reputation=agent.reputation,
			valid=False,
			note="质量预估不达标",
		);
	return Bid(
		agent_id=agent.agent_id,
		price=price,
		latency_ms=latency_ms,
		quality=quality_claim,
		reputation=agent.reputation,
	);


def 最低价中标(task, bids, rng):
	# 一阶密封拍卖：有效报价里最便宜的直接拿单，按自己的报价结算。
	# task 和 rng 只是为了和其他机制保持同一个签名。
	# 返回中标 Bid 和成交价，没人能投时返回 None 和 0。
	valid_bids = [bid for bid in bids if bid.valid];
	if not valid_bids:
		return None, 0.0;
	winner = min(valid_bids, key=lambda bid: bid.price);
	return winner, winner.price;


def 维克里中标(task, bids, rng):
	# 二阶密封拍卖：最低价中标，但按第二低价付钱，虚低报价赚不到便宜。
	# 参数同最低价机制。
	# 返回中标 Bid 和成交价，只有一家投标时按自家报价结算。
	valid_bids = sorted((bid for bid in bids if bid.valid), key=lambda bid: bid.price);
	if not valid_bids:
		return None, 0.0;
	winner = valid_bids[0];
	payment = valid_bids[1].price if len(valid_bids) > 1 else winner.price;
	return winner, round(payment, 2);


def 综合评分中标(task, bids, rng):
	# 价格、质量、时延、信誉各自归一化后加权，分最高的拿单。
	# 参数同最低价机制；返回中标 Bid 和成交价。
	valid_bids = [bid for bid in bids if bid.valid];
	if not valid_bids:
		return None, 0.0;
	max_latency = max(bid.latency_ms for bid in valid_bids);
	for bid in valid_bids:
		price_score = 1.0 - bid.price / task.budget;
		latency_score = 1.0 - bid.latency_ms / max_latency if max_latency > 0 else 1.0;
		bid.score = round(
			V_综合评分权重["价格"] * price_score
			+ V_综合评分权重["质量"] * bid.quality
			+ V_综合评分权重["时延"] * latency_score
			+ V_综合评分权重["信誉"] * bid.reputation,
			4,
		);
	winner = max(valid_bids, key=lambda bid: bid.score);
	return winner, winner.price;


V_拍卖机制 = {"最低价": 最低价中标, "维克里": 维克里中标, "综合评分": 综合评分中标};  # 机制名到拍卖函数的注册表。


def 结算(task, winner, payment, agents, bids, rng):
	# 中标者交活，质量过了门槛全额结算，没过就只付六成并安排返工。
	# task 是任务，winner 是中标报价，payment 是成交价，agents 是全部 Agent，
	# bids 是本轮报价，rng 制造交付波动；返回本轮记录。
	agent = next(item for item in agents if item.agent_id == winner.agent_id);
	actual_quality = round(min(0.99, max(0.0, winner.quality + rng.uniform(-0.05, 0.05))), 3);
	actual_cost = round(task.workload * agent.unit_cost, 2);
	agent.load = round(min(1.0, agent.load + task.workload / agent.capacity), 3);
	agent.win_count += 1;
	if actual_quality >= task.min_quality:
		agent.revenue += payment;
		agent.cost += actual_cost;
		agent.reputation = round(min(1.0, agent.reputation + V_成功信用加分), 3);
		return RoundRecord(
			round_index=task.arrive_round,
			task_id=task.task_id,
			capability=task.capability,
			value=task.value,
			winner=agent.agent_id,
			payment=payment,
			cost=actual_cost,
			quality=actual_quality,
			bid_count=len(bids),
			valid_count=sum(1 for bid in bids if bid.valid),
			blocked_count=sum(1 for bid in bids if (not bid.valid and "异常低价" in bid.note)),
			status="完成",
		);
	agent.revenue += round(payment * (1.0 - V_返工扣款比例), 2);
	agent.cost += actual_cost;
	agent.reputation = round(max(0.0, agent.reputation - V_返工信用扣分), 3);
	return RoundRecord(
		round_index=task.arrive_round,
		task_id=task.task_id,
		capability=task.capability,
		value=task.value,
		winner=agent.agent_id,
		payment=round(payment * (1.0 - V_返工扣款比例), 2),
		cost=actual_cost,
		quality=actual_quality,
		bid_count=len(bids),
		valid_count=sum(1 for bid in bids if bid.valid),
		blocked_count=sum(1 for bid in bids if (not bid.valid and "异常低价" in bid.note)),
		status="返工",
		note="质量未达门槛",
	);


def 兜底处理(task, agents, bids, rng):
	# 场上没有有效报价时让备用 Agent 接单，备用也顶不住就挂人工。
	# 参数含义同结算；返回本轮记录。
	backup = next((item for item in agents if item.is_backup), None);
	blocked_count = sum(1 for bid in bids if (not bid.valid and "异常低价" in bid.note));
	if backup is None or backup.load >= V_备用Agent负载上限 or task.capability not in backup.skills:
		return RoundRecord(
			round_index=task.arrive_round,
			task_id=task.task_id,
			capability=task.capability,
			value=task.value,
			winner="人工",
			payment=0.0,
			cost=0.0,
			quality=0.0,
			bid_count=len(bids),
			valid_count=0,
			blocked_count=blocked_count,
			status="人工接管",
			note="无可用 Agent，转人工处理",
		);
	payment = round(task.workload * backup.unit_cost * V_备用Agent加价率, 2);
	actual_quality = round(min(0.99, backup.skills[task.capability] * backup.quality + rng.uniform(-0.03, 0.03)), 3);
	actual_cost = round(task.workload * backup.unit_cost, 2);
	backup.load = round(min(1.0, backup.load + task.workload / backup.capacity), 3);
	backup.win_count += 1;
	backup.revenue += payment;
	backup.cost += actual_cost;
	return RoundRecord(
		round_index=task.arrive_round,
		task_id=task.task_id,
		capability=task.capability,
		value=task.value,
		winner=backup.agent_id,
		payment=payment,
		cost=actual_cost,
		quality=actual_quality,
		bid_count=len(bids),
		valid_count=0,
		blocked_count=blocked_count,
		status="兜底",
		note="无人竞标，备用 Agent 接单",
	);


def 执行一轮拍卖(task, agents, mechanism_name, rng):
	# 收齐报价，扣掉恶意低价者的信誉，再交给指定机制选人。
	# task 是任务，agents 是竞标方，mechanism_name 选机制，rng 是随机数源；返回本轮记录。
	bids = [];
	for agent in agents:
		if agent.is_backup or agent.load >= 1.0:
			continue;
		bids.append(计算报价(agent, task, rng));
	for agent in agents:
		if any((not bid.valid and bid.agent_id == agent.agent_id and "异常低价" in bid.note) for bid in bids):
			agent.reputation = round(max(0.0, agent.reputation - V_恶意报价扣分), 3);
	winner, payment = V_拍卖机制[mechanism_name](task, bids, rng);
	if winner is None:
		return 兜底处理(task, agents, bids, rng);
	return 结算(task, winner, payment, agents, bids, rng);


def 运行市场(tasks, agents, mechanism_name, seed, rounds):
	# 一轮一轮往下推：先卸掉部分旧负载，再拍本轮到期的任务，返工的排到下一轮。
	# tasks、agents 是现场数据，mechanism_name 选机制，seed 定随机性，rounds 控轮数。
	# 返回 RunReport，逐轮记录、负载曲线和汇总指标都在里面。
	rng = random.Random(seed);
	records = [];
	load_history = [];
	for round_index in range(1, rounds + 1):
		for agent in agents:
			agent.load = round(agent.load * V_负载衰减比例, 3);
			if agent.reputation < V_信誉红线:
				agent.reputation = round(min(V_信誉红线, agent.reputation + V_信誉恢复速度), 3);
		due_tasks = [task for task in tasks if task.arrive_round == round_index];
		for task in due_tasks:
			record = 执行一轮拍卖(task, agents, mechanism_name, rng);
			record.round_index = round_index;
			records.append(record);
			if record.status == "返工" and task.attempts < V_最大返工次数 and round_index < rounds:
				task.attempts += 1;
				task.arrive_round = round_index + 1;
		load_history.append({agent.agent_id: agent.load for agent in agents});
	report = RunReport(
		mechanism=mechanism_name,
		records=records,
		load_history=load_history,
		agents=agents,
	);
	report.metrics = 汇总统计(report);
	return report;


def 汇总统计(report):
	# 把逐轮记录压成一组指标，控制台和图表都从这里取数。
	# report 是一次模拟的结果；返回指标字典。
	records = report.records;
	final_status = {record.task_id: record.status for record in records};
	counts = {"完成": 0, "返工": 0, "兜底": 0, "人工接管": 0};
	for record in records:
		counts[record.status] = counts.get(record.status, 0) + 1;
	paid_records = [record for record in records if record.payment > 0];
	settled_records = [record for record in records if record.status in ("完成", "兜底")];
	load_std = [statistics.pstdev(list(snapshot.values())) for snapshot in report.load_history];
	task_total = len(final_status);
	return {
		"任务数": task_total,
		"完成": counts["完成"],
		"返工": counts["返工"],
		"兜底": counts["兜底"],
		"人工接管": counts["人工接管"],
		"交付率": (counts["完成"] + counts["兜底"]) / task_total if task_total else 0.0,
		"总报酬": round(sum(record.payment for record in records), 2),
		"总成本": round(sum(record.cost for record in records), 2),
		"平台效用": round(sum(record.value - record.payment for record in settled_records), 2),
		"平均成交价": round(sum(record.payment for record in paid_records) / len(paid_records), 2) if paid_records else 0.0,
		"平均质量": round(sum(record.quality for record in settled_records) / len(settled_records), 3) if settled_records else 0.0,
		"拦截恶意报价": sum(record.blocked_count for record in records),
		"平均负载标准差": round(statistics.fmean(load_std), 3) if load_std else 0.0,
		"未完成": sum(1 for status in final_status.values() if status not in ("完成", "兜底")),
	};


# 控制台输出

def 对齐文本(text, width):
	# 中文在控制台占两个字符宽，按这个规则补空格，表格才不会错位。
	# text 是要打印的内容，width 是目标宽度；返回补好空格的字符串。
	display_width = sum((2 if ord(char) > 127 else 1) for char in str(text));
	return str(text) + " " * max(0, width - display_width);


def 打印项目信息():
	# 开场只交代项目名和流程，第一屏别全是表格。
	print("——" * 50);
	print("市场拍卖式多智能体任务调度仿真！");
	print("一轮流程：发布任务 → 收集报价 → 比价中标 → 交付结算！");
	print("——" * 50);


def 打印Agent预览(agents):
	# 把几个 Agent 的家底摊开，成本、时延、负载一眼能看出差别。
	print("【Agent 初始状态】");
	print(对齐文本("编号", 14) + 对齐文本("单位成本", 10) + 对齐文本("基础时延ms", 12) + 对齐文本("质量", 10) + 对齐文本("容量", 8) + 对齐文本("负载", 10) + 对齐文本("信誉", 10) + "擅长能力");
	for agent in agents:
		best_skill = max(agent.skills, key=agent.skills.get);
		print(对齐文本(agent.agent_id, 14) + f"{agent.unit_cost:>10.2f}" + f"{agent.base_latency_ms:>12.0f}" + f"{agent.quality:>10.2f}" + f"{agent.capacity:>8.0f}" + f"{agent.load:>10.2f}" + f"{agent.reputation:>10.2f}  " + best_skill);
	print("——" * 50);


def 打印任务预览(tasks):
	# 抽前六条任务，看看能力、预算和质量门槛大概落在什么区间。
	print(f"【任务样本，共 {len(tasks)} 条，展示前 6 条】");
	print(对齐文本("编号", 10) + 对齐文本("能力", 12) + 对齐文本("工作量", 10) + 对齐文本("预算", 10) + 对齐文本("质量门槛", 12) + "到达轮次");
	for task in tasks[:6]:
		print(对齐文本(task.task_id, 10) + 对齐文本(task.capability, 12) + f"{task.workload:>10.2f}" + f"{task.budget:>10.2f}" + f"{task.min_quality:>14.2f}" + f"{task.arrive_round:>10d}");
	print("……");
	print("——" * 50);


def 打印单场结果(report):
	# 单个机制的明细只打前 12 轮，后面用汇总指标收尾，免得刷屏。
	print(f"【{report.mechanism}机制 · 竞价明细，前 12 轮】");
	print(对齐文本("轮次", 8) + 对齐文本("任务", 10) + 对齐文本("能力", 12) + 对齐文本("中标", 14) + 对齐文本("成交价", 10) + 对齐文本("质量", 8) + "状态");
	for record in report.records[:12]:
		print(对齐文本(record.round_index, 8) + 对齐文本(record.task_id, 10) + 对齐文本(record.capability, 12) + 对齐文本(record.winner, 14) + f"{record.payment:>10.2f}" + f"{record.quality:>10.2f}  " + record.status);
	print(f"交付 {report.metrics['完成']} 单，返工 {report.metrics['返工']} 次，兜底 {report.metrics['兜底']} 次，人工接管 {report.metrics['人工接管']} 次，未完成 {report.metrics['未完成']} 单！");
	print(f"总报酬 {report.metrics['总报酬']:.2f}，实际成本 {report.metrics['总成本']:.2f}，平台效用 {report.metrics['平台效用']:.2f}，平均成交价 {report.metrics['平均成交价']:.2f}！");
	print(f"平均交付质量 {report.metrics['平均质量']:.3f}，平均负载标准差 {report.metrics['平均负载标准差']:.3f}，拦截恶意报价 {report.metrics['拦截恶意报价']} 次！");


def 打印Agent结算(agents):
	# 每个 Agent 的中标次数、收入和成本，谁赚谁亏一目了然。
	print("【Agent 结算汇总】");
	print(对齐文本("编号", 14) + f"{'中标次数':>9}" + f"{'报酬收入':>11}" + f"{'实际成本':>11}" + f"{'净收益':>10}" + f"{'信誉':>8}" + f"{'负载':>8}");
	for agent in agents:
		print(对齐文本(agent.agent_id, 14) + f"{agent.win_count:>10d}" + f"{agent.revenue:>12.2f}" + f"{agent.cost:>12.2f}" + f"{agent.revenue - agent.cost:>11.2f}" + f"{agent.reputation:>9.2f}" + f"{agent.load:>9.2f}");


def 打印机制对比(reports):
	# 三个机制并排比成本、质量、效用和负载，选型时看这张表。
	print("【三种拍卖机制对比】");
	print(对齐文本("机制", 14) + f"{'总报酬':>10}" + f"{'总成本':>10}" + f"{'均价':>9}" + f"{'平均质量':>10}" + f"{'交付率':>9}" + f"{'平台效用':>10}" + f"{'负载标准差':>12}" + f"{'拦截':>7}" + f"{'兜底':>7}");
	for name, report in reports.items():
		print(对齐文本(name, 14) + f"{report.metrics['总报酬']:>10.2f}" + f"{report.metrics['总成本']:>10.2f}" + f"{report.metrics['平均成交价']:>9.2f}" + f"{report.metrics['平均质量']:>10.3f}" + f"{report.metrics['交付率']:>10.1%}" + f"{report.metrics['平台效用']:>10.2f}" + f"{report.metrics['平均负载标准差']:>12.3f}" + f"{report.metrics['拦截恶意报价']:>7d}" + f"{report.metrics['兜底']:>7d}");


# 图表

def 绘制图表(reports):
	# 出两张图：机制之间横着比一比，再单独看各 Agent 的负载和收益。
	# reports 是按机制名索引的结果；图片写到 output 目录。
	plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"];
	plt.rcParams["axes.unicode_minus"] = False;
	V_输出目录.mkdir(parents=True, exist_ok=True);
	mechanism_names = list(reports);
	positions = list(range(len(mechanism_names)));
	completed = [reports[name].metrics["完成"] for name in mechanism_names];
	reworked = [reports[name].metrics["返工"] for name in mechanism_names];
	fallback = [reports[name].metrics["兜底"] for name in mechanism_names];
	manual = [reports[name].metrics["人工接管"] for name in mechanism_names];
	quality_values = [reports[name].metrics["平均质量"] for name in mechanism_names];
	figure, axes = plt.subplots(1, 3, figsize=(16, 4.8));
	axes[0].bar([pos - 0.18 for pos in positions], [reports[name].metrics["总报酬"] for name in mechanism_names], width=0.36, color=V_图表配色[0], label="总报酬");
	axes[0].bar([pos + 0.18 for pos in positions], [reports[name].metrics["总成本"] for name in mechanism_names], width=0.36, color=V_图表配色[1], label="总成本");
	axes[0].set_xticks(positions);
	axes[0].set_xticklabels(mechanism_names);
	axes[0].set_title("成交金额与实际成本");
	axes[0].grid(axis="y", alpha=0.25);
	axes[0].legend();
	bottom_rework = completed;
	bottom_fallback = [completed[index] + reworked[index] for index in positions];
	bottom_manual = [bottom_fallback[index] + fallback[index] for index in positions];
	axes[1].bar(mechanism_names, completed, color=V_图表配色[3], label="完成");
	axes[1].bar(mechanism_names, reworked, bottom=bottom_rework, color=V_图表配色[2], label="返工");
	axes[1].bar(mechanism_names, fallback, bottom=bottom_fallback, color=V_图表配色[0], label="兜底");
	axes[1].bar(mechanism_names, manual, bottom=bottom_manual, color=V_图表配色[1], label="人工接管");
	axes[1].set_title("任务结果分布");
	axes[1].legend();
	bars = axes[2].bar(mechanism_names, quality_values, color=V_图表配色[4]);
	axes[2].set_ylim(0, 1.05);
	axes[2].set_title("平均交付质量");
	for bar, value in zip(bars, quality_values):
		axes[2].text(bar.get_x() + bar.get_width() / 2, value + 0.02, f"{value:.3f}", ha="center", fontsize=10);
	figure.tight_layout();
	figure.savefig(V_输出目录 / "机制对比.png", dpi=160);
	plt.close(figure);
	main_report = reports.get("综合评分") or next(iter(reports.values()));
	agent_names = [agent.agent_id for agent in main_report.agents];
	agent_positions = list(range(len(agent_names)));
	profits = [round(agent.revenue - agent.cost, 2) for agent in main_report.agents];
	figure, axes = plt.subplots(1, 2, figsize=(15, 4.8));
	for index, agent in enumerate(main_report.agents):
		axes[0].plot(range(1, len(main_report.load_history) + 1), [snapshot[agent.agent_id] for snapshot in main_report.load_history], label=agent.agent_id, color=V_图表配色[index % len(V_图表配色)], linewidth=1.8);
	axes[0].set_title(f"各 Agent 负载曲线（{main_report.mechanism}）");
	axes[0].set_xlabel("轮次");
	axes[0].set_ylabel("负载");
	axes[0].set_ylim(0, 1.05);
	axes[0].grid(alpha=0.25);
	axes[0].legend(ncol=2, fontsize=9);
	axes[1].bar([pos - 0.18 for pos in agent_positions], [agent.win_count for agent in main_report.agents], width=0.36, color=V_图表配色[0], label="中标次数");
	axes[1].set_ylabel("中标次数");
	second_axis = axes[1].twinx();
	second_axis.bar([pos + 0.18 for pos in agent_positions], profits, width=0.36, color=V_图表配色[1], label="净收益");
	second_axis.set_ylabel("净收益");
	axes[1].set_xticks(agent_positions);
	axes[1].set_xticklabels(agent_names, rotation=15);
	axes[1].set_title("Agent 中标次数与净收益");
	axes[1].legend(loc="upper left");
	second_axis.legend(loc="upper right");
	figure.tight_layout();
	figure.savefig(V_输出目录 / "负载与收益.png", dpi=160);
	plt.close(figure);
	print(f"图表已保存到 {V_输出目录}！");


# 命令行入口

def 解析参数():
	# 命令行只留四个开关：轮次、种子、机制、要不要出图。
	# 返回 argparse 解析结果。
	parser = argparse.ArgumentParser(description="市场拍卖式多智能体任务调度仿真");
	parser.add_argument("--rounds", type=int, default=V_任务总轮次, help="模拟轮次，默认 24");
	parser.add_argument("--seed", type=int, default=V_随机种子, help="随机种子，默认固定值保证可复现");
	parser.add_argument("--mechanism", choices=["最低价", "维克里", "综合评分", "对比"], default="对比", help="拍卖机制，默认对比三种");
	parser.add_argument("--no-chart", action="store_true", help="只输出控制台表格，不生成图表");
	return parser.parse_args();


def main():
	# 造任务、跑选中的机制、打印结果，最后出图。
	args = 解析参数();
	打印项目信息();
	task_templates = 生成任务列表(random.Random(args.seed), args.rounds);
	打印Agent预览(构建异构Agent());
	打印任务预览(task_templates);
	mechanism_names = list(V_拍卖机制) if args.mechanism == "对比" else [args.mechanism];
	reports = {};
	for mechanism_name in mechanism_names:
		fresh_agents = 构建异构Agent();
		report = 运行市场(复制任务列表(task_templates), fresh_agents, mechanism_name, args.seed, args.rounds);
		reports[mechanism_name] = report;
		print("——" * 50);
		打印单场结果(report);
		if len(mechanism_names) == 1 or mechanism_name == "综合评分":
			打印Agent结算(fresh_agents);
	if len(reports) > 1:
		print("——" * 50);
		打印机制对比(reports);
	if not args.no_chart:
		绘制图表(reports);
		print("——" * 50);
		print("✅ 全部模拟完成，图表已保存到 output 目录！");
	else:
		print("——" * 50);
		print("✅ 全部模拟完成，本次未生成图表！");


if __name__ == "__main__":
	main();