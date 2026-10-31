/* SPDX-License-Identifier: GPL-2.0 */
#define _GNU_SOURCE
#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <scx/common.h>

#include "flowcausal_v2.h"
#include "scx_flowcausal_v2.bpf.skel.h"

/* sched_ext_ops.flags 中 SCX_OPS_ENQ_LAST 的稳定 ABI 位。 */
#define FLOW_SCX_OPS_ENQ_LAST (1ULL << 1)

static volatile sig_atomic_t exit_req;
static const char *pin_dir = "/sys/fs/bpf/flowcausal_v2";
static bool pin_enabled = true;
static bool verbose;
static bool quiet;
static bool detailed_accounting;
static bool shared_ordinary_path;
static bool flow_policy_enabled = true;
static bool progress_enabled = true;
static bool interactive_enabled;
static bool background_enabled = true;
static bool locality_enabled = true;
static bool parallel_width_enabled = true;
static bool periodic_fair_accounting = true;
static bool equal_weight_fair_fastpath = true;
static bool session_runnable_width = true;
static bool cross_session_role_dispatch = true;
static bool cross_session_progress_when_few_runnable;
static bool demand_aware_borrow;
static bool logarithmic_demand_share;
static bool balanced_demand_share;
static bool adaptive_session_weight;
static bool automatic_session_weight;
static __u64 automatic_weight_window_ms = 100;
static bool automatic_weight_repeat_wait;
static bool require_runnable_wait;
static bool sleeping_descendant_progress;
static bool sustained_tool_weight;
static __u64 sustained_tool_weight_multiplier = 2;
static bool sustained_tool_observe_only;
static bool descendant_wakeup_observe;
static bool descendant_wakeup_light_observe;
static bool descendant_wakeup_progress;
static bool enforce_cpu_max;
static __u32 decision_mode = FLOW_V2_DECISION_RELATION;
static __u32 cpu_first;
static __u32 cpu_count = 8;
static __u64 slice_us;
static __u64 ordinary_wakeup_credit_us = 20000;
static bool adaptive_wakeup_credit;
static bool sleep_bounded_wakeup_credit;
static bool threaded_wakeup_credit;
static bool local_futex_credit = true;
static bool independent_weight_service = true;
static bool observe_weight_service;
static bool preempt_confirmed_wait;

static void on_signal(int sig)
{
	(void)sig;
	exit_req = 1;
}

static int libbpf_log(enum libbpf_print_level level, const char *fmt, va_list ap)
{
	if (level == LIBBPF_DEBUG && !verbose)
		return 0;
	return vfprintf(stderr, fmt, ap);
}

static int mkdir_p(const char *path)
{
	char buf[PATH_MAX];
	size_t len;

	if (!path || !*path || snprintf(buf, sizeof(buf), "%s", path) >= (int)sizeof(buf))
		return -EINVAL;
	len = strlen(buf);
	if (len > 1 && buf[len - 1] == '/')
		buf[len - 1] = '\0';
	for (char *p = buf + 1; *p; p++) {
		if (*p != '/')
			continue;
		*p = '\0';
		if (mkdir(buf, 0755) && errno != EEXIST)
			return -errno;
		*p = '/';
	}
	if (mkdir(buf, 0755) && errno != EEXIST)
		return -errno;
	return 0;
}

static int pin_one(struct bpf_map *map, const char *name)
{
	char path[PATH_MAX];

	if (snprintf(path, sizeof(path), "%s/%s", pin_dir, name) >= (int)sizeof(path))
		return -ENAMETOOLONG;
	if (unlink(path) && errno != ENOENT)
		return -errno;
	return bpf_obj_pin(bpf_map__fd(map), path);
}

static void unpin_one(const char *name)
{
	char path[PATH_MAX];

	if (snprintf(path, sizeof(path), "%s/%s", pin_dir, name) < (int)sizeof(path))
		unlink(path);
}

static int pin_runtime_maps(struct scx_flowcausal_v2 *skel)
{
	int err;

	if ((err = mkdir_p(pin_dir)))
		return err;
	/* 前五个别名让现有实验运行程序能够等待新调度器就绪。 */
	if ((err = pin_one(skel->maps.flow_v2_task_meta, "task_ctx")) ||
	    (err = pin_one(skel->maps.active_tool_by_cgroup, "active_tool_by_cgroup")) ||
	    (err = pin_one(skel->maps.session_config, "session_policy")) ||
	    (err = pin_one(skel->maps.session_stats, "session_stats")) ||
	    (err = pin_one(skel->maps.global_stats, "global_stats")) ||
	    (err = pin_one(skel->maps.events, "events")) ||
	    (err = pin_one(skel->maps.flow_v2_task_meta, "flow_v2_task_meta")) ||
	    (err = pin_one(skel->maps.session_config, "session_config")) ||
	    (err = pin_one(skel->maps.active_session_slots, "active_session_slots")) ||
	    (err = pin_one(skel->maps.session_runtime, "session_runtime")) ||
	    (err = pin_one(skel->maps.session_adaptive_state,
			   "session_adaptive_state")) ||
	    (err = pin_one(skel->maps.session_fair_runtime, "session_fair_runtime")) ||
	    (err = pin_one(skel->maps.width_state, "width_state")) ||
	    (err = pin_one(skel->maps.control_state, "control_state")) ||
	    (err = pin_one(skel->maps.tool_config, "tool_config")) ||
	    (err = pin_one(skel->maps.tool_runtime, "tool_runtime")))
		return err;
	return 0;
}

static void unpin_runtime_maps(void)
{
	static const char *const names[] = {
		"task_ctx", "session_policy", "session_stats", "global_stats",
		"events", "flow_v2_task_meta", "session_config", "tool_config",
		"tool_runtime", "active_session_slots", "active_tool_by_cgroup",
		"session_runtime", "session_adaptive_state", "session_fair_runtime",
		"width_state", "control_state",
	};

	for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); i++)
		unpin_one(names[i]);
	rmdir(pin_dir);
}

static int parse_u64(const char *name, const char *text, __u64 *out)
{
	char *end = NULL;
	unsigned long long value;

	errno = 0;
	value = strtoull(text, &end, 10);
	if (errno || !end || *end) {
		fprintf(stderr, "%s 参数无效：%s\n", name, text);
		return -EINVAL;
	}
	*out = (__u64)value;
	return 0;
}

static int parse_cpu_range(const char *text)
{
	char *end = NULL;
	unsigned long first, last;

	errno = 0;
	first = strtoul(text, &end, 10);
	if (errno || !end)
		return -EINVAL;
	if (*end == '\0') {
		last = first;
	} else if (*end == '-') {
		char *last_end = NULL;

		errno = 0;
		last = strtoul(end + 1, &last_end, 10);
		if (errno || !last_end || *last_end)
			return -EINVAL;
	} else {
		return -EINVAL;
	}
	if (last < first || last > UINT32_MAX)
		return -EINVAL;
	cpu_first = (__u32)first;
	cpu_count = (__u32)(last - first + 1);
	return 0;
}

static void usage(const char *prog)
{
	fprintf(stderr,
		"用法：%s [选项]\n"
		"  --pin-dir 路径              映射固定目录\n"
		"  --managed-cpus 起点-终点    FlowCausal 管理的连续处理器范围\n"
		"  --slice-us 微秒             会话轮转时间片\n"
		"  --ordinary-wakeup-credit-us N 普通任务睡醒后提前排队的上限，0..20000 微秒（实验）\n"
		"  --adaptive-wakeup-credit    按线程上次睡眠前实际使用的 CPU 时间调整提前量（实验）\n"
		"  --sleep-bounded-wakeup-credit 普通任务的提前量不超过本次实际睡眠时间（实验）\n"
		"  --independent-weight-service 独立核算临时加权期间的执行时间（默认）\n"
		"  --legacy-weight-service    使用旧计费方式（仅用于对照）\n"
		"  --no-local-futex-credit     关闭进程内锁唤醒限制（仅用于对照）\n"
		"  --observe-weight-service   记录临时加权的两种计费方式（诊断）\n"
		"  --local-futex-credit        限制进程内短计算线程的重复 futex 唤醒提前量（默认）\n"
		"  --threaded-wakeup-credit   仅在同一进程线程数超过 CPU 数时限制睡眠提前量（实验）\n"
		"  --preempt-confirmed-wait  已确认的等待对象醒来并取得预算后请求及时运行（实验）\n"
		"  --two-level                 使用默认的两级工作流调度路径\n"
		"  --shared-process-queue      仅用于对照的全局进程队列路径\n"
		"  --grouping-only             只保留工作流分组，用于机制消融\n"
		"  --relation-policy           使用工具边界和内核等待关系（默认）\n"
		"  --stage-policy              只使用固定执行阶段规则\n"
		"  --tool-policy               只使用同步或异步工具边界\n"
		"  --kernel-policy             只使用处理器等待和可运行数量\n"
		"  --manual-policy             只采用人工标出的任务\n"
		"  --no-progress              关闭关键推进\n"
		"  --interactive              启用短交互处理（默认关闭）\n"
		"  --no-interactive           关闭短交互处理（默认）\n"
		"  --no-background            关闭后台任务延后\n"
		"  --no-locality              关闭近期处理器复用\n"
		"  --no-parallel-width        关闭动态并行度控制\n"
		"  --virtual-time-fairness-only 只使用控制组虚拟运行时间，不做周期处理器时间核算\n"
		"  --no-equal-weight-fair-fastpath 等权工作流仍使用完整周期核算（用于对照）\n"
		"  --session-runnable-width   按工作流实际可运行进程数决定并行度（默认）\n"
		"  --tool-runnable-width      仅在标注为并行的工具内启用动态并行度\n"
		"  --cross-session-roles      返回任务跨工作流有限提前（默认）\n"
		"  --session-local-roles      返回任务只在所属工作流内提前\n"
		"  --cross-session-progress-when-few-runnable 仅在所属工作流当前最多两个可运行进程时跨工作流提前等待对象\n"
		"  --demand-aware-borrow      保护各工作流后按可运行任务数量借用处理器\n"
		"  --logarithmic-demand-share 按可运行任务数的对数分配剩余处理器\n"
		"  --balanced-demand-share    工作流结束后将释放的处理器时间一半按当前需求分配\n"
		"  --adaptive-session-weight  同步工具等待对象发生处理器排队时临时提高工作流权重\n"
		"  --automatic-session-weight 不使用同步/异步标记，根据实际等待和排队自动选择一个工作流提高权重\n"
		"  --automatic-weight-window-ms N 自动提高权重的最长持续时间，默认 100 毫秒\n"
		"  --automatic-weight-repeat-wait 长等待恢复一段时间后凭新的排队再次参加选择\n"
		"  --require-runnable-wait    被等待进程实际排队后才允许关键推进\n"
		"  --sleeping-descendant-progress 启用上层进程睡眠期间的后代短时提前实验\n"
		"  --no-sleeping-descendant-progress 关闭上述实验（默认）\n"
		"  --sustained-tool-weight    根据同一工具后代的持续运行与排队，有限增加整个会话的处理器时间\n"
		"  --sustained-tool-weight-multiplier N 持续等待候选的临时权重倍数，默认 2\n"
		"  --sustained-tool-observe   采集相同信息但不据此改变调度，用于区分观测开销\n"
		"  --descendant-wakeup-observe 记录同一工具内后代进程唤醒睡眠上层进程，不改变调度\n"
		"  --descendant-wakeup-light-observe 使用正式低开销识别，只计数、不改变调度\n"
		"  --descendant-wakeup-progress 让实际唤醒过上层且经历排队的进程在工作流内提前一次\n"
		"  --enforce-cpu-max          为设置处理器时间上限的会话启用强制重排\n"
		"  --detailed-accounting       开启逐次调度统计（仅用于机制核对）\n"
		"  --no-pin                    不固定运行时映射\n"
		"  --verbose                   输出 libbpf 调试信息\n"
		"  --quiet                     不输出周期信息\n",
		prog);
}

static int parse_args(int argc, char **argv)
{
	for (int i = 1; i < argc; i++) {
		const char *arg = argv[i];

		if (!strcmp(arg, "--help")) {
			usage(argv[0]);
			exit(0);
		} else if (!strcmp(arg, "--pin-dir") && i + 1 < argc) {
			pin_dir = argv[++i];
		} else if (!strcmp(arg, "--managed-cpus") && i + 1 < argc) {
			if (parse_cpu_range(argv[++i]))
				return -EINVAL;
		} else if (!strcmp(arg, "--slice-us") && i + 1 < argc) {
			if (parse_u64(arg, argv[++i], &slice_us))
				return -EINVAL;
		} else if (!strcmp(arg, "--adaptive-wakeup-credit")) {
			adaptive_wakeup_credit = true;
		} else if (!strcmp(arg, "--sleep-bounded-wakeup-credit")) {
			sleep_bounded_wakeup_credit = true;
		} else if (!strcmp(arg, "--threaded-wakeup-credit")) {
			sleep_bounded_wakeup_credit = true;
			threaded_wakeup_credit = true;
		} else if (!strcmp(arg, "--independent-weight-service")) {
			independent_weight_service = true;
		} else if (!strcmp(arg, "--legacy-weight-service")) {
			independent_weight_service = false;
		} else if (!strcmp(arg, "--no-local-futex-credit")) {
			local_futex_credit = false;
		} else if (!strcmp(arg, "--observe-weight-service")) {
			observe_weight_service = true;
		} else if (!strcmp(arg, "--local-futex-credit")) {
			local_futex_credit = true;
		} else if (!strcmp(arg, "--preempt-confirmed-wait")) {
			preempt_confirmed_wait = true;
		} else if (!strcmp(arg, "--ordinary-wakeup-credit-us") && i + 1 < argc) {
			if (parse_u64(arg, argv[++i], &ordinary_wakeup_credit_us) ||
			    ordinary_wakeup_credit_us > 20000)
				return -EINVAL;
		} else if (!strcmp(arg, "--no-pin")) {
			pin_enabled = false;
		} else if (!strcmp(arg, "--detailed-accounting")) {
			detailed_accounting = true;
		} else if (!strcmp(arg, "--two-level")) {
			shared_ordinary_path = false;
		} else if (!strcmp(arg, "--shared-process-queue")) {
			shared_ordinary_path = true;
		} else if (!strcmp(arg, "--grouping-only")) {
			flow_policy_enabled = false;
		} else if (!strcmp(arg, "--relation-policy")) {
			decision_mode = FLOW_V2_DECISION_RELATION;
		} else if (!strcmp(arg, "--stage-policy")) {
			decision_mode = FLOW_V2_DECISION_STAGE;
		} else if (!strcmp(arg, "--tool-policy")) {
			decision_mode = FLOW_V2_DECISION_TOOL;
		} else if (!strcmp(arg, "--kernel-policy")) {
			decision_mode = FLOW_V2_DECISION_KERNEL;
		} else if (!strcmp(arg, "--manual-policy")) {
			decision_mode = FLOW_V2_DECISION_MANUAL;
		} else if (!strcmp(arg, "--no-progress")) {
			progress_enabled = false;
		} else if (!strcmp(arg, "--interactive")) {
			interactive_enabled = true;
		} else if (!strcmp(arg, "--no-interactive")) {
			interactive_enabled = false;
		} else if (!strcmp(arg, "--no-background")) {
			background_enabled = false;
		} else if (!strcmp(arg, "--no-locality")) {
			locality_enabled = false;
		} else if (!strcmp(arg, "--no-parallel-width")) {
			parallel_width_enabled = false;
		} else if (!strcmp(arg, "--virtual-time-fairness-only")) {
			periodic_fair_accounting = false;
		} else if (!strcmp(arg, "--no-equal-weight-fair-fastpath")) {
			equal_weight_fair_fastpath = false;
		} else if (!strcmp(arg, "--session-runnable-width")) {
			session_runnable_width = true;
		} else if (!strcmp(arg, "--tool-runnable-width")) {
			session_runnable_width = false;
		} else if (!strcmp(arg, "--cross-session-roles")) {
			cross_session_role_dispatch = true;
		} else if (!strcmp(arg, "--session-local-roles")) {
			cross_session_role_dispatch = false;
		} else if (!strcmp(arg,
				  "--cross-session-progress-when-few-runnable")) {
			cross_session_progress_when_few_runnable = true;
		} else if (!strcmp(arg, "--demand-aware-borrow")) {
			demand_aware_borrow = true;
		} else if (!strcmp(arg, "--logarithmic-demand-share")) {
			demand_aware_borrow = true;
			logarithmic_demand_share = true;
		} else if (!strcmp(arg, "--balanced-demand-share")) {
			demand_aware_borrow = true;
			balanced_demand_share = true;
		} else if (!strcmp(arg, "--adaptive-session-weight")) {
			adaptive_session_weight = true;
		} else if (!strcmp(arg, "--automatic-session-weight")) {
			adaptive_session_weight = true;
			automatic_session_weight = true;
		} else if (!strcmp(arg, "--automatic-weight-window-ms") &&
			   i + 1 < argc) {
			if (parse_u64(arg, argv[++i], &automatic_weight_window_ms) ||
			    !automatic_weight_window_ms)
				return -EINVAL;
		} else if (!strcmp(arg, "--automatic-weight-repeat-wait")) {
			automatic_weight_repeat_wait = true;
		} else if (!strcmp(arg, "--require-runnable-wait")) {
			require_runnable_wait = true;
		} else if (!strcmp(arg, "--sleeping-descendant-progress")) {
			sleeping_descendant_progress = true;
		} else if (!strcmp(arg, "--no-sleeping-descendant-progress")) {
			sleeping_descendant_progress = false;
		} else if (!strcmp(arg, "--sustained-tool-weight")) {
			sustained_tool_weight = true;
			adaptive_session_weight = true;
			automatic_session_weight = true;
			automatic_weight_repeat_wait = true;
		} else if (!strcmp(arg, "--sustained-tool-weight-multiplier") &&
			   i + 1 < argc) {
			if (parse_u64(arg, argv[++i],
				      &sustained_tool_weight_multiplier) ||
			    sustained_tool_weight_multiplier < 2 ||
			    sustained_tool_weight_multiplier > 8)
				return -EINVAL;
		} else if (!strcmp(arg, "--sustained-tool-observe")) {
			sustained_tool_observe_only = true;
			adaptive_session_weight = true;
			automatic_session_weight = true;
		} else if (!strcmp(arg, "--descendant-wakeup-observe")) {
			descendant_wakeup_observe = true;
		} else if (!strcmp(arg, "--descendant-wakeup-light-observe")) {
			descendant_wakeup_light_observe = true;
		} else if (!strcmp(arg, "--descendant-wakeup-progress")) {
			descendant_wakeup_progress = true;
		} else if (!strcmp(arg, "--enforce-cpu-max")) {
			enforce_cpu_max = true;
		} else if (!strcmp(arg, "--verbose")) {
			verbose = true;
		} else if (!strcmp(arg, "--quiet")) {
			quiet = true;
		} else {
			fprintf(stderr, "无法识别的参数：%s\n", arg);
			return -EINVAL;
		}
	}
	if ((logarithmic_demand_share || balanced_demand_share) &&
	    adaptive_session_weight) {
		fprintf(stderr, "按需求分配不能与动态权重同时启用\n");
		return -EINVAL;
	}
	return cpu_count ? 0 : -EINVAL;
}

int main(int argc, char **argv)
{
	struct scx_flowcausal_v2 *skel = NULL;
	struct flow_v2_control_state control = {};
	struct bpf_link *link = NULL;
	__u32 control_key = 0;
	__u64 ecode;
	int err;

	libbpf_set_print(libbpf_log);
	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);
	if ((err = parse_args(argc, argv)))
		return 2;

	skel = SCX_OPS_OPEN(flowcausal_v2_ops, scx_flowcausal_v2);
	if (enforce_cpu_max)
		skel->struct_ops.flowcausal_v2_ops->flags |= FLOW_SCX_OPS_ENQ_LAST;
	skel->rodata->managed_cpu_first = cpu_first;
	skel->rodata->managed_cpu_count = cpu_count;
	/* 尾部是关系定义，不随机器处理器数扩大。 */
	skel->rodata->nr_cpus = cpu_count;
	skel->rodata->cgrp_slice_ns = slice_us ? slice_us * 1000 :
		__COMPAT_ENUM_OR_ZERO("scx_public_consts", "SCX_SLICE_DFL");
	skel->rodata->progress_slice_ns = 2ULL * 1000 * 1000;
	skel->rodata->interactive_slice_ns = 1ULL * 1000 * 1000;
	skel->rodata->detailed_accounting = detailed_accounting;
	skel->rodata->shared_ordinary_path = shared_ordinary_path;
	skel->rodata->flow_policy_enabled = flow_policy_enabled;
	skel->rodata->progress_enabled = progress_enabled;
	skel->rodata->interactive_enabled = interactive_enabled;
	skel->rodata->background_enabled = background_enabled;
	skel->rodata->locality_enabled = locality_enabled;
	skel->rodata->parallel_width_enabled = parallel_width_enabled;
	skel->rodata->ordinary_wakeup_credit_ns = ordinary_wakeup_credit_us * 1000;
	skel->rodata->adaptive_wakeup_credit = adaptive_wakeup_credit;
	skel->rodata->sleep_bounded_wakeup_credit = sleep_bounded_wakeup_credit;
	skel->rodata->threaded_wakeup_credit = threaded_wakeup_credit;
	skel->rodata->local_futex_credit = local_futex_credit;
	skel->rodata->independent_weight_service = independent_weight_service;
	skel->rodata->observe_weight_service = observe_weight_service;
	skel->rodata->preempt_confirmed_wait = preempt_confirmed_wait;
	skel->rodata->periodic_fair_accounting = periodic_fair_accounting;
	skel->rodata->equal_weight_fair_fastpath = equal_weight_fair_fastpath;
	skel->rodata->session_runnable_width = session_runnable_width;
	skel->rodata->cross_session_role_dispatch = cross_session_role_dispatch;
	skel->rodata->cross_session_progress_when_few_runnable =
		cross_session_progress_when_few_runnable;
	skel->rodata->demand_aware_borrow = demand_aware_borrow;
	skel->rodata->logarithmic_demand_share = logarithmic_demand_share;
	skel->rodata->balanced_demand_share = balanced_demand_share;
	skel->rodata->adaptive_session_weight = adaptive_session_weight;
	skel->rodata->automatic_session_weight = automatic_session_weight;
	skel->rodata->automatic_weight_window_ns =
		automatic_weight_window_ms * 1000 * 1000;
	skel->rodata->automatic_weight_repeat_wait =
		automatic_weight_repeat_wait;
	skel->rodata->require_runnable_wait = require_runnable_wait;
	skel->rodata->sleeping_descendant_progress = sleeping_descendant_progress;
	skel->rodata->sustained_tool_weight = sustained_tool_weight;
	skel->rodata->sustained_tool_weight_multiplier =
		(__u32)sustained_tool_weight_multiplier;
	skel->rodata->sustained_tool_observe_only =
		sustained_tool_observe_only;
	skel->rodata->descendant_wakeup_observe = descendant_wakeup_observe;
	skel->rodata->descendant_wakeup_light_observe =
		descendant_wakeup_light_observe;
	skel->rodata->descendant_wakeup_progress = descendant_wakeup_progress;
	skel->rodata->decision_mode = decision_mode;
	bpf_program__set_autoload(skel->progs.flow_v2_local_futex_wake, local_futex_credit);
	/* scx_flatcg 中被 v2 回调替换的程序不参与装载。 */
	bpf_program__set_autoload(skel->progs.fcg_select_cpu, false);
	bpf_program__set_autoload(skel->progs.fcg_enqueue, false);
	bpf_program__set_autoload(skel->progs.fcg_dispatch, false);
	bpf_program__set_autoload(skel->progs.fcg_runnable, false);
	bpf_program__set_autoload(skel->progs.fcg_running, false);
	bpf_program__set_autoload(skel->progs.fcg_stopping, false);
	bpf_program__set_autoload(skel->progs.fcg_quiescent, false);
	bpf_program__set_autoload(skel->progs.fcg_init_task, false);
	bpf_program__set_autoload(skel->progs.fcg_cgroup_exit, false);
	bpf_program__set_autoload(skel->progs.fcg_init, false);
	/*
	 * fork/exec/exit 是任务来源关系本身，不是调试记账。正式运行也必须
	 * 始终挂载；detailed_accounting 只控制逐事件输出和累计开销统计。
	 */

	SCX_OPS_LOAD(skel, flowcausal_v2_ops, scx_flowcausal_v2, uei);
	control.managed_cpu_count = cpu_count;
	if (bpf_map_update_elem(bpf_map__fd(skel->maps.control_state),
				&control_key, &control, BPF_ANY)) {
		err = -errno;
		fprintf(stderr, "初始化会话登记状态失败：%s\n", strerror(errno));
		goto out;
	}
	if (pin_enabled && (err = pin_runtime_maps(skel))) {
		fprintf(stderr, "固定运行时映射失败：%s\n", strerror(-err));
		goto out;
	}
	link = SCX_OPS_ATTACH(skel, flowcausal_v2_ops, scx_flowcausal_v2);
	if (!quiet) {
		printf("flowcausal_v2 ready cpus=%u-%u path=%s slice_us=%llu pin_dir=%s\n",
		       cpu_first, cpu_first + cpu_count - 1,
		       shared_ordinary_path ? "共享" : "两级",
		       (unsigned long long)(skel->rodata->cgrp_slice_ns / 1000),
		       pin_enabled ? pin_dir : "关闭");
		fflush(stdout);
	}
	while (!exit_req && !UEI_EXITED(skel, uei))
		sleep(1);

	ecode = UEI_REPORT(skel, uei);
	err = (int)ecode;
out:
	bpf_link__destroy(link);
	if (observe_weight_service && skel) {
		unsigned long long observed = 0, legacy = 0, applied = 0;
		for (unsigned int cpu = 0; cpu < 256; cpu++) {
			observed += skel->bss->local_schedule_stats[cpu][4];
			legacy += skel->bss->local_schedule_stats[cpu][5];
			applied += skel->bss->local_schedule_stats[cpu][6];
		}
		printf("weight_interval_ns=%llu weight_legacy_charge_ns=%llu weight_applied_charge_ns=%llu\n",
		       observed, legacy, applied);
	}
	if (local_futex_credit && skel) {
		unsigned long long marked = 0, limited = 0;

		for (unsigned int cpu = 0; cpu < 256; cpu++) {
			marked += skel->bss->local_schedule_stats[cpu][0];
			limited += skel->bss->local_schedule_stats[cpu][1];
		}
		printf("local_futex_marked=%llu local_futex_limited=%llu\n", marked, limited);
	}
	if (preempt_confirmed_wait && skel)
		printf("confirmed_wait_preemptions=%llu\n",
		       (unsigned long long)skel->bss->confirmed_wait_preemptions);
	if (pin_enabled)
		unpin_runtime_maps();
	scx_flowcausal_v2__destroy(skel);
	return err < 0 ? 1 : err;
}
