/* SPDX-License-Identifier: GPL-2.0 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <flowcausal_v2.h>

enum operation {
	OP_SESSION_OPEN,
	OP_SESSION_CLOSE,
	OP_TOOL_BEGIN,
	OP_TOOL_UPDATE,
	OP_TOOL_END,
	OP_TOOL_DEPENDENCY,
	OP_STATS,
};

static volatile sig_atomic_t exit_req;
static enum operation operation = OP_SESSION_OPEN;
static const char *pin_dir = "/sys/fs/bpf/flowcausal_v2";
static const char *session_name = "flowcausal:default";
static const char *cgroup_path;
static const char *serve_socket_path;
static const char *resource_output_path;
static char session_cgroup_path[PATH_MAX];
static __u64 session_id;
static __u64 cgroup_id;
static __u64 tool_id;
static __u64 parent_tool_id;
static __u64 tool_generation;
static __u64 sequence = 1;
static __u64 policy_window_ms = 100;
static __u64 boost_budget_ms = 10;
static __u64 progress_bias_us = 750;
static __u64 interactive_bias_us = 250;
static __u64 background_delay_us = 20000;
/*
 * 默认仍只保留一个推进名额，但允许这个名额沿已经确认的连续等待
 * 关系向下转交两次；这不会把多个后代同时纳入推进范围。
 */
static __u32 max_progress_group = 1;
static __u32 max_progress_depth = 2;
static __u32 root_pid;
/* 长驻控制程序的会话根；不能被后续工具更新中的 root_pid 覆盖。 */
static __u32 session_root_pid;
static __u32 weight = 100;
static __u32 role = FLOW_V2_ROLE_NORMAL;
static __u32 stage = FLOW_V2_STAGE_UNKNOWN;
static bool explicit_role;
static bool once;
static bool print_events;
static bool root_is_scope_only;
static bool manual_progress;
static __u64 process_started_ns;

static int task_fd = -1;
static int active_tool_fd = -1;
static int session_fd = -1;
static int session_runtime_fd = -1;
static int session_adaptive_state_fd = -1;
static int session_fair_runtime_fd = -1;
static int width_state_fd = -1;
static int control_state_fd = -1;
static int tool_fd = -1;
static int tool_runtime_fd = -1;
static int stats_fd = -1;
static int global_fd = -1;
static int events_fd = -1;
static int slots_fd = -1;
static __u32 claimed_slot = FLOW_V2_ACTIVE_SESSION_SLOTS;

/* 只索引本会话仍活动的工具；历史配置继续留在内核表中核对代次。
 * 长驻控制程序复用这个索引，单次 CLI 调用则从内核表恢复一次。
 * 选择时仍读取内核配置，不缓存工具角色或代次。 */
static __u64 active_tool_ids[FLOW_V2_MAX_TOOLS];
static size_t nr_active_tools;
static __u64 active_index_session;
static __u64 active_index_sequence;
static bool active_index_valid;

/* 与内核映射值保持相同布局；锁字段由内核使用，用户态只写入零值。 */
struct flow_v2_session_runtime_user {
	__u32 lock;
	__u32 padding;
	__u64 window_start_ns;
	__u64 boost_used_ns;
	__u64 quota_period_start_ns;
	__u64 quota_used_ns;
	__u64 quota_reserved_ns;
	__u32 runnable_tasks;
	__u32 running_tasks;
	__u32 reserved_tasks;
	__u32 peak_running_tasks;
	__u32 current_width;
	__u32 quota_throttled;
};

struct flow_v2_session_adaptive_state_user {
	__u32 active_blocking_waiters;
	__u32 runnable_wait_targets;
	__u32 adaptive_weight_active;
	__u32 effective_weight;
	__u32 runnable_tasks;
	__u32 wait_pressure_seen;
	__u64 adaptive_weight_started_ns;
	__u64 wait_pressure_generation;
	__u64 granted_wait_generation;
	__u64 automatic_weight_deadline_ns;
	__u64 last_grant_order;
};

struct flow_v2_fair_runtime_user {
	__u32 lock;
	__u32 padding;
	__u64 window_id;
	__u64 runnable_epoch;
	__u64 used_ns;
	__u64 reserved_ns;
};

#define FLOW_V2_MAX_CLIENTS 32
#define FLOW_V2_CLIENT_BUFFER 1024

struct flow_v2_client_tool {
	__u64 tool_id;
	__u64 generation;
	struct flow_v2_client_tool *next;
};

struct flow_v2_client {
	int fd;
	__u32 peer_pid;
	char buffer[FLOW_V2_CLIENT_BUFFER];
	size_t used;
	struct flow_v2_client_tool *tools;
};

static int event_callback(void *ctx, void *data, size_t size);

static __u64 now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (__u64)ts.tv_sec * 1000000000ULL + (__u64)ts.tv_nsec;
}

static void write_resource_usage(void)
{
	struct rusage usage;
	FILE *file;
	__u64 elapsed_ns;
	double user_sec;
	double system_sec;

	if (!resource_output_path || getrusage(RUSAGE_SELF, &usage))
		return;
	file = fopen(resource_output_path, "w");
	if (!file)
		return;
	elapsed_ns = now_ns() - process_started_ns;
	user_sec = (double)usage.ru_utime.tv_sec +
		(double)usage.ru_utime.tv_usec / 1000000.0;
	system_sec = (double)usage.ru_stime.tv_sec +
		(double)usage.ru_stime.tv_usec / 1000000.0;
	fprintf(file,
		"{\"elapsed_ns\":%llu,\"user_sec\":%.6f,"
		"\"system_sec\":%.6f,\"cpu_sec\":%.6f,"
		"\"max_rss_kb\":%ld,\"voluntary_switches\":%ld,"
		"\"involuntary_switches\":%ld}\n",
		(unsigned long long)elapsed_ns, user_sec, system_sec,
		user_sec + system_sec, usage.ru_maxrss, usage.ru_nvcsw,
		usage.ru_nivcsw);
	fclose(file);
}

static void on_signal(int sig)
{
	(void)sig;
	exit_req = 1;
}

static __u64 stable_id(const char *text)
{
	__u64 value = 1469598103934665603ULL;

	for (const unsigned char *p = (const unsigned char *)text; *p; p++) {
		value ^= *p;
		value *= 1099511628211ULL;
	}
	return value ?: 1;
}

static int parse_u64(const char *name, const char *text, __u64 *out)
{
	char *end = NULL;
	unsigned long long value;

	errno = 0;
	value = strtoull(text, &end, 0);
	if (errno || !end || *end) {
		fprintf(stderr, "%s 参数无效：%s\n", name, text);
		return -EINVAL;
	}
	*out = (__u64)value;
	return 0;
}

static int parse_u32(const char *name, const char *text, __u32 *out)
{
	__u64 value;
	int err = parse_u64(name, text, &value);

	if (err || value > UINT32_MAX)
		return -EINVAL;
	*out = (__u32)value;
	return 0;
}

static int parse_tool_execution_mode(const char *text, __u32 *out)
{
	/*
	 * 套接字事件只报告智能体是否同步等待工具返回。命令用途标签属于
	 * 基准分析数据，不能在这里翻译成调度类别。同步工具先走普通路径，
	 * 需要推进的返回任务完全由内核中的等待关系和可运行等待识别。
	 */
	if (!strcmp(text, "synchronous"))
		*out = FLOW_V2_ROLE_NORMAL;
	else if (!strcmp(text, "asynchronous"))
		*out = FLOW_V2_ROLE_BACKGROUND;
	else
		return -EINVAL;
	return 0;
}

static int parse_tool_stage(const char *text, __u32 *out)
{
	if (!strcasecmp(text, "unknown") || !strcasecmp(text, "shell"))
		*out = FLOW_V2_STAGE_UNKNOWN;
	else if (!strcasecmp(text, "prepare") || !strcasecmp(text, "retry") ||
		 !strcasecmp(text, "retry_probe"))
		*out = FLOW_V2_STAGE_PREPARE;
	else if (!strcasecmp(text, "inspect") || !strcasecmp(text, "search") ||
		 !strcasecmp(text, "result"))
		*out = FLOW_V2_STAGE_INSPECT;
	else if (!strcasecmp(text, "edit"))
		*out = FLOW_V2_STAGE_EDIT;
	else if (!strcasecmp(text, "build") || !strcasecmp(text, "compile"))
		*out = FLOW_V2_STAGE_BUILD;
	else if (!strcasecmp(text, "test"))
		*out = FLOW_V2_STAGE_TEST;
	else if (!strcasecmp(text, "data"))
		*out = FLOW_V2_STAGE_DATA;
	else if (!strcasecmp(text, "package") ||
		 !strcasecmp(text, "package_background"))
		*out = FLOW_V2_STAGE_PACKAGE;
	else if (!strcasecmp(text, "service"))
		*out = FLOW_V2_STAGE_SERVICE;
	else
		return -EINVAL;
	return 0;
}

static __u32 behavior_from_stage(__u32 value)
{
	if (value == FLOW_V2_STAGE_BUILD || value == FLOW_V2_STAGE_TEST ||
	    value == FLOW_V2_STAGE_DATA || value == FLOW_V2_STAGE_PACKAGE)
		return FLOW_V2_BEHAVIOR_PARALLEL;
	if (value == FLOW_V2_STAGE_SERVICE)
		return FLOW_V2_BEHAVIOR_SERVICE;
	if (value == FLOW_V2_STAGE_PREPARE || value == FLOW_V2_STAGE_INSPECT ||
	    value == FLOW_V2_STAGE_EDIT)
		return FLOW_V2_BEHAVIOR_SERIAL;
	return FLOW_V2_BEHAVIOR_UNKNOWN;
}

static enum operation parse_operation(const char *text)
{
	if (!strcmp(text, "session-open"))
		return OP_SESSION_OPEN;
	if (!strcmp(text, "session-close"))
		return OP_SESSION_CLOSE;
	if (!strcmp(text, "tool-begin"))
		return OP_TOOL_BEGIN;
	if (!strcmp(text, "tool-update"))
		return OP_TOOL_UPDATE;
	if (!strcmp(text, "tool-end"))
		return OP_TOOL_END;
	if (!strcmp(text, "tool-dependency"))
		return OP_TOOL_DEPENDENCY;
	if (!strcmp(text, "stats"))
		return OP_STATS;
	fprintf(stderr, "未知操作：%s\n", text);
	exit(2);
}

static void usage(const char *prog)
{
	fprintf(stderr,
		"用法：%s [--operation 操作] [参数]\n"
		"操作：session-open、session-close、tool-begin、tool-update、tool-end、tool-dependency、stats\n"
		"常用参数：\n"
		"  --pin-dir 路径       调度器固定映射目录\n"
		"  --session 名称       会话稳定名称\n"
		"  --session-id 编号    显式会话编号\n"
		"  --root-pid 进程号    会话或工具根进程\n"
		"  --cgroup 路径        会话 cgroup 路径\n"
		"  --tool-id 编号       工具调用编号\n"
		"  --parent-tool-id 编号 父工具调用编号\n"
		"  --generation 编号    本次工具调用的代次编号\n"
		"  --sequence 序号      单调递增事件序号\n"
		"  --execution-mode 模式 同步或异步工具执行\n"
		"  --stage 阶段          工具执行阶段，只作为任务特征\n"
		"  --manual-progress     人工标注该工具决定返回（只用于对照实验）\n"
		"  --weight 权重         会话调度权重\n"
		"  --policy-window-ms 毫秒  提前量统计周期\n"
		"  --boost-budget-ms 毫秒   每周期最大提前量\n"
		"  --max-vtime-boost-us 微秒 单个任务最大虚拟时间提前量\n"
		"  --background-delay-us 微秒 旧任务最长连续延后时间\n"
		"  --max-progress-group 数量  一轮等待最多推进的进程数，默认 1\n"
		"  --max-progress-depth 层数  等待关系最多跟随的派生层数，默认 2\n"
		"  --serve-socket 路径  通过本地套接字长驻接收工具事件\n"
		"  --resource-output 路径 记录本进程资源用量（仅用于实验）\n"
		"  --events             持续输出内核关系事件\n"
		"  --once               写入后立即退出\n",
		prog);
}

static int parse_args(int argc, char **argv)
{
	for (int i = 1; i < argc; i++) {
		const char *arg = argv[i];

		if (!strcmp(arg, "--help")) {
			usage(argv[0]);
			exit(0);
		} else if (!strcmp(arg, "--operation") && i + 1 < argc) {
			operation = parse_operation(argv[++i]);
		} else if (!strcmp(arg, "--pin-dir") && i + 1 < argc) {
			pin_dir = argv[++i];
		} else if (!strcmp(arg, "--session") && i + 1 < argc) {
			session_name = argv[++i];
		} else if (!strcmp(arg, "--session-id") && i + 1 < argc) {
			if (parse_u64(arg, argv[++i], &session_id))
				return -EINVAL;
		} else if (!strcmp(arg, "--cgroup-id") && i + 1 < argc) {
			if (parse_u64(arg, argv[++i], &cgroup_id))
				return -EINVAL;
		} else if (!strcmp(arg, "--cgroup") && i + 1 < argc) {
			cgroup_path = argv[++i];
		} else if (!strcmp(arg, "--serve-socket") && i + 1 < argc) {
			serve_socket_path = argv[++i];
		} else if (!strcmp(arg, "--resource-output") && i + 1 < argc) {
			resource_output_path = argv[++i];
		} else if (!strcmp(arg, "--root-pid") && i + 1 < argc) {
			if (parse_u32(arg, argv[++i], &root_pid))
				return -EINVAL;
		} else if (!strcmp(arg, "--tool-id") && i + 1 < argc) {
			if (parse_u64(arg, argv[++i], &tool_id))
				return -EINVAL;
		} else if (!strcmp(arg, "--parent-tool-id") && i + 1 < argc) {
			if (parse_u64(arg, argv[++i], &parent_tool_id))
				return -EINVAL;
		} else if (!strcmp(arg, "--generation") && i + 1 < argc) {
			if (parse_u64(arg, argv[++i], &tool_generation))
				return -EINVAL;
		} else if (!strcmp(arg, "--sequence") && i + 1 < argc) {
			if (parse_u64(arg, argv[++i], &sequence))
				return -EINVAL;
		} else if (!strcmp(arg, "--execution-mode") && i + 1 < argc) {
			if (parse_tool_execution_mode(argv[++i], &role))
				return -EINVAL;
			explicit_role = true;
		} else if (!strcmp(arg, "--stage") && i + 1 < argc) {
			if (parse_tool_stage(argv[++i], &stage))
				return -EINVAL;
		} else if (!strcmp(arg, "--manual-progress")) {
			manual_progress = true;
		} else if (!strcmp(arg, "--role") ||
			   !strcmp(arg, "--auto-classify") ||
			   !strcmp(arg, "--no-auto-classify")) {
			fprintf(stderr, "%s 已从 FlowCausal v2 接口删除；调度只依据工具执行关系和内核等待关系\n", arg);
			return -EINVAL;
		} else if (!strcmp(arg, "--weight") && i + 1 < argc) {
			if (parse_u32(arg, argv[++i], &weight) || !weight)
				return -EINVAL;
		} else if (!strcmp(arg, "--policy-window-ms") && i + 1 < argc) {
			if (parse_u64(arg, argv[++i], &policy_window_ms))
				return -EINVAL;
		} else if (!strcmp(arg, "--boost-budget-ms") && i + 1 < argc) {
			if (parse_u64(arg, argv[++i], &boost_budget_ms))
				return -EINVAL;
		} else if (!strcmp(arg, "--max-vtime-boost-us") && i + 1 < argc) {
			if (parse_u64(arg, argv[++i], &progress_bias_us))
				return -EINVAL;
		} else if (!strcmp(arg, "--interactive-bias-us") && i + 1 < argc) {
			if (parse_u64(arg, argv[++i], &interactive_bias_us))
				return -EINVAL;
		} else if (!strcmp(arg, "--background-delay-us") && i + 1 < argc) {
			if (parse_u64(arg, argv[++i], &background_delay_us))
				return -EINVAL;
		} else if (!strcmp(arg, "--max-progress-group") && i + 1 < argc) {
			if (parse_u32(arg, argv[++i], &max_progress_group) ||
			    !max_progress_group)
				return -EINVAL;
		} else if (!strcmp(arg, "--max-progress-depth") && i + 1 < argc) {
			if (parse_u32(arg, argv[++i], &max_progress_depth))
				return -EINVAL;
		} else if (!strcmp(arg, "--events")) {
			print_events = true;
		} else if (!strcmp(arg, "--once")) {
			once = true;
		} else {
			fprintf(stderr, "无法识别的参数：%s\n", arg);
			return -EINVAL;
		}
	}
	if (!session_id)
		session_id = stable_id(session_name);
	return 0;
}

static int open_pinned(const char *canonical, const char *compat)
{
	char path[PATH_MAX];
	int fd;

	if (snprintf(path, sizeof(path), "%s/%s", pin_dir, canonical) >= (int)sizeof(path))
		return -ENAMETOOLONG;
	fd = bpf_obj_get(path);
	if (fd >= 0 || !compat)
		return fd;
	if (snprintf(path, sizeof(path), "%s/%s", pin_dir, compat) >= (int)sizeof(path))
		return -ENAMETOOLONG;
	return bpf_obj_get(path);
}

static int open_maps(void)
{
	task_fd = open_pinned("flow_v2_task_meta", "task_ctx");
	active_tool_fd = open_pinned("active_tool_by_cgroup", NULL);
	session_fd = open_pinned("session_config", "session_policy");
	session_runtime_fd = open_pinned("session_runtime", NULL);
	session_adaptive_state_fd = open_pinned("session_adaptive_state", NULL);
	session_fair_runtime_fd = open_pinned("session_fair_runtime", NULL);
	width_state_fd = open_pinned("width_state", NULL);
	control_state_fd = open_pinned("control_state", NULL);
	tool_fd = open_pinned("tool_config", NULL);
	tool_runtime_fd = open_pinned("tool_runtime", NULL);
	stats_fd = open_pinned("session_stats", NULL);
	global_fd = open_pinned("global_stats", NULL);
	events_fd = open_pinned("events", NULL);
	slots_fd = open_pinned("active_session_slots", NULL);
	if (task_fd < 0 || active_tool_fd < 0 || session_fd < 0 ||
	    session_runtime_fd < 0 || session_adaptive_state_fd < 0 ||
	    session_fair_runtime_fd < 0 ||
	    width_state_fd < 0 || control_state_fd < 0 || tool_fd < 0 ||
	    tool_runtime_fd < 0 || stats_fd < 0 || global_fd < 0 ||
	    events_fd < 0 || slots_fd < 0) {
		fprintf(stderr, "无法打开 %s 下的 FlowCausal v2 映射：%s\n",
			pin_dir, strerror(errno));
		return -errno;
	}
	return 0;
}

static unsigned runnable_demand_score(unsigned demand)
{
	return demand ? 32U - (unsigned)__builtin_clz(demand) : 0;
}

/*
 * 会话建立和关闭远少于任务调度。把“全部基础权重是否相同”放在这里
 * 计算，内核即可在等权工作流中省去重复的周期公平核算；若登记状态
 * 正在并发变化，数量不一致时宁可继续使用完整核算。
 */
static void refresh_registered_weight_summary(
	struct flow_v2_control_state *state)
{
	struct flow_v2_session_config cfg;
	__u64 key;
	__u64 next;
	__u32 common = 0;
	__u32 count = 0;
	bool equal = true;
	int err;

	if (!state)
		return;
	err = bpf_map_get_next_key(session_fd, NULL, &next);
	while (!err) {
		key = next;
		if (!bpf_map_lookup_elem(session_fd, &key, &cfg) &&
		    (cfg.flags & FLOW_V2_SESSION_ENABLED)) {
			__u32 weight = cfg.weight ?: 1;

			if (!count)
				common = weight;
			else if (weight != common)
				equal = false;
			count++;
		}
		err = bpf_map_get_next_key(session_fd, &key, &next);
	}
	state->common_weight = common;
	state->registered_weights_equal = count && equal &&
		count == state->registered_sessions;
}

static int adjust_control_state(int session_delta, long long weight_delta,
				long long disabled_runnable_weight_delta,
				int disabled_session_delta,
				int disabled_demand_delta,
				int disabled_running_delta)
{
	struct flow_v2_control_state state = {};
	int disabled_score_delta = disabled_demand_delta > 0 ?
		(int)runnable_demand_score((unsigned)disabled_demand_delta) :
		disabled_demand_delta < 0 ?
		-(int)runnable_demand_score((unsigned)-disabled_demand_delta) : 0;
	char lock_path[128];
	__u32 key = 0;
	int lock_fd;
	int err = 0;

	if (snprintf(lock_path, sizeof(lock_path),
		     "/tmp/flowcausal-v2-width-%llx.lock",
		     (unsigned long long)stable_id(pin_dir)) >= (int)sizeof(lock_path))
		return -ENAMETOOLONG;
	lock_fd = open(lock_path, O_CREAT | O_CLOEXEC | O_RDWR, 0600);
	if (lock_fd < 0)
		return -errno;
	if (flock(lock_fd, LOCK_EX)) {
		err = -errno;
		goto out;
	}
	if (bpf_map_lookup_elem(control_state_fd, &key, &state)) {
		err = -errno;
		goto unlock;
	}
	if (session_delta < 0 && state.registered_sessions < (unsigned)-session_delta) {
		err = -ERANGE;
		goto unlock;
	}
	if (weight_delta < 0 && state.total_weight < (unsigned long long)-weight_delta) {
		err = -ERANGE;
		goto unlock;
	}
	if (disabled_runnable_weight_delta < 0 &&
	    state.disabled_runnable_weight <
	    (unsigned long long)-disabled_runnable_weight_delta) {
		err = -ERANGE;
		goto unlock;
	}
	if (disabled_session_delta < 0 && state.disabled_runnable_sessions <
	    (unsigned)-disabled_session_delta) {
		err = -ERANGE;
		goto unlock;
	}
	if (disabled_demand_delta < 0 && state.disabled_runnable_demand <
	    (unsigned)-disabled_demand_delta) {
		err = -ERANGE;
		goto unlock;
	}
	if (disabled_score_delta < 0 && state.disabled_runnable_score <
	    (unsigned)-disabled_score_delta) {
		err = -ERANGE;
		goto unlock;
	}
	if (disabled_running_delta < 0 && state.disabled_running_tasks <
	    (unsigned)-disabled_running_delta) {
		err = -ERANGE;
		goto unlock;
	}
	state.registered_sessions += session_delta;
	if (state.registered_sessions > state.peak_registered_sessions)
		state.peak_registered_sessions = state.registered_sessions;
	state.total_weight += weight_delta;
	state.disabled_runnable_weight += disabled_runnable_weight_delta;
	state.disabled_runnable_sessions += disabled_session_delta;
	state.disabled_runnable_demand += disabled_demand_delta;
	state.disabled_runnable_score += disabled_score_delta;
	state.disabled_running_tasks += disabled_running_delta;
	refresh_registered_weight_summary(&state);
	if (disabled_runnable_weight_delta || disabled_session_delta ||
	    disabled_demand_delta || disabled_score_delta ||
	    disabled_running_delta)
		state.runnable_epoch++;
	if (bpf_map_update_elem(control_state_fd, &key, &state, BPF_ANY))
		err = -errno;
unlock:
	flock(lock_fd, LOCK_UN);
out:
	close(lock_fd);
	return err;
}

static int claim_session_slot(void)
{
	for (__u32 slot = 0; slot < FLOW_V2_ACTIVE_SESSION_SLOTS; slot++) {
		if (!bpf_map_update_elem(slots_fd, &slot, &cgroup_id, BPF_NOEXIST)) {
			claimed_slot = slot;
			return 0;
		}
		if (errno != EEXIST)
			return -errno;
	}
	return -ENOSPC;
}

static void release_session_slots_for_cgroup(__u64 target_cgroup)
{
	for (__u32 slot = 0; slot < FLOW_V2_ACTIVE_SESSION_SLOTS; slot++) {
		__u64 value;

		if (!bpf_map_lookup_elem(slots_fd, &slot, &value) &&
		    value == target_cgroup)
			bpf_map_delete_elem(slots_fd, &slot);
	}
}

static void release_session_slot(void)
{
	__u64 value;

	if (claimed_slot < FLOW_V2_ACTIVE_SESSION_SLOTS &&
	    !bpf_map_lookup_elem(slots_fd, &claimed_slot, &value) &&
	    value == cgroup_id)
		bpf_map_delete_elem(slots_fd, &claimed_slot);
	claimed_slot = FLOW_V2_ACTIVE_SESSION_SLOTS;
}

static int cgroup_id_from_path(const char *path, __u64 *out)
{
	struct stat st;

	if (stat(path, &st))
		return -errno;
	*out = (__u64)st.st_ino;
	return 0;
}

static int cgroup_id_from_pid(__u32 pid, __u64 *out, char *resolved,
			      size_t resolved_size)
{
	char proc_path[64];
	char line[PATH_MAX];
	FILE *file;

	if (snprintf(proc_path, sizeof(proc_path), "/proc/%u/cgroup", pid) >=
	    (int)sizeof(proc_path))
		return -ENAMETOOLONG;
	file = fopen(proc_path, "r");
	if (!file)
		return -errno;
	while (fgets(line, sizeof(line), file)) {
		char *path;
		size_t len;

		if (strncmp(line, "0::", 3))
			continue;
		path = line + 3;
		len = strcspn(path, "\r\n");
		path[len] = '\0';
		if (snprintf(resolved, resolved_size, "/sys/fs/cgroup%s", path) >=
		    (int)resolved_size) {
			fclose(file);
			return -ENAMETOOLONG;
		}
		fclose(file);
		return cgroup_id_from_path(resolved, out);
	}
	fclose(file);
	return -ENOENT;
}

static int find_cgroup_by_session(__u64 wanted_session, __u64 *out)
{
	struct flow_v2_session_config cfg;
	__u64 key, next;
	int err;

	err = bpf_map_get_next_key(session_fd, NULL, &next);
	while (!err) {
		key = next;
		if (!bpf_map_lookup_elem(session_fd, &key, &cfg) &&
		    cfg.session_id == wanted_session) {
			*out = key;
			return 0;
		}
		err = bpf_map_get_next_key(session_fd, &key, &next);
	}
	return -ENOENT;
}

static int resolve_cgroup(void)
{
	int err;

	if (cgroup_id)
		return 0;
	if (cgroup_path) {
		if (snprintf(session_cgroup_path, sizeof(session_cgroup_path), "%s",
			     cgroup_path) >= (int)sizeof(session_cgroup_path))
			return -ENAMETOOLONG;
		return cgroup_id_from_path(cgroup_path, &cgroup_id);
	}
	if (root_pid) {
		err = cgroup_id_from_pid(root_pid, &cgroup_id,
					 session_cgroup_path,
					 sizeof(session_cgroup_path));
		return err;
	}
	return find_cgroup_by_session(session_id, &cgroup_id);
}

static int read_cgroup_cpu_weight(__u32 *out)
{
	char path[PATH_MAX];
	unsigned int value;
	FILE *file;

	if (!session_cgroup_path[0])
		return -ENOENT;
	if (snprintf(path, sizeof(path), "%s/cpu.weight", session_cgroup_path) >=
	    (int)sizeof(path))
		return -ENAMETOOLONG;
	file = fopen(path, "r");
	if (!file)
		return -errno;
	if (fscanf(file, "%u", &value) != 1 || value < 1 || value > 10000) {
		fclose(file);
		return -EINVAL;
	}
	fclose(file);
	*out = value;
	return 0;
}

static int read_cgroup_cpu_max(__u64 *quota_ns, __u64 *period_ns)
{
	char path[PATH_MAX];
	char quota_text[32];
	unsigned long long period_us;
	unsigned long long quota_us = 0;
	FILE *file;

	if (!session_cgroup_path[0])
		return -ENOENT;
	if (snprintf(path, sizeof(path), "%s/cpu.max", session_cgroup_path) >=
	    (int)sizeof(path))
		return -ENAMETOOLONG;
	file = fopen(path, "r");
	if (!file)
		return -errno;
	if (fscanf(file, "%31s %llu", quota_text, &period_us) != 2 || !period_us) {
		fclose(file);
		return -EINVAL;
	}
	if (strcmp(quota_text, "max")) {
		char *end = NULL;

		errno = 0;
		quota_us = strtoull(quota_text, &end, 10);
		if (errno || !end || *end || !quota_us) {
			fclose(file);
			return -EINVAL;
		}
	}
	fclose(file);
	*quota_ns = (__u64)quota_us * 1000;
	*period_ns = (__u64)period_us * 1000;
	return 0;
}

static int pid_namespace_inode(__u32 pid, ino_t *inode)
{
	char path[64];
	struct stat st;

	if (snprintf(path, sizeof(path), "/proc/%u/ns/pid", pid) >=
	    (int)sizeof(path))
		return -ENAMETOOLONG;
	if (stat(path, &st))
		return -errno;
	*inode = st.st_ino;
	return 0;
}

static int innermost_namespace_pid(__u32 host_pid, __u32 *namespace_pid)
{
	char path[64];
	char line[512];
	FILE *file;

	if (snprintf(path, sizeof(path), "/proc/%u/status", host_pid) >=
	    (int)sizeof(path))
		return -ENAMETOOLONG;
	file = fopen(path, "r");
	if (!file)
		return -errno;
	while (fgets(line, sizeof(line), file)) {
		char *cursor;
		unsigned long value;

		if (strncmp(line, "NSpid:", 6))
			continue;
		cursor = line + 6;
		while (*cursor) {
			char *end;

			errno = 0;
			value = strtoul(cursor, &end, 10);
			if (end == cursor) {
				cursor++;
				continue;
			}
			if (errno || value > UINT32_MAX) {
				fclose(file);
				return -EINVAL;
			}
			*namespace_pid = (__u32)value;
			cursor = end;
		}
		fclose(file);
		return *namespace_pid ? 0 : -ENOENT;
	}
	fclose(file);
	return -ENOENT;
}

static int resolve_namespace_pid(__u32 peer_pid, __u32 namespace_pid,
				 __u32 *host_pid)
{
	char path[PATH_MAX];
	char line[64];
	ino_t peer_namespace;
	FILE *file;
	int err;

	if (!namespace_pid || !session_cgroup_path[0])
		return -ENOENT;
	err = pid_namespace_inode(peer_pid, &peer_namespace);
	if (err)
		return err;
	if (snprintf(path, sizeof(path), "%s/cgroup.procs", session_cgroup_path) >=
	    (int)sizeof(path))
		return -ENAMETOOLONG;
	file = fopen(path, "r");
	if (!file)
		return -errno;
	while (fgets(line, sizeof(line), file)) {
		unsigned long candidate_value;
		__u32 candidate_namespace_pid = 0;
		ino_t candidate_namespace;
		char *end;

		errno = 0;
		candidate_value = strtoul(line, &end, 10);
		if (errno || end == line || candidate_value > UINT32_MAX)
			continue;
		if (pid_namespace_inode((__u32)candidate_value,
					&candidate_namespace) ||
		    candidate_namespace != peer_namespace ||
		    innermost_namespace_pid((__u32)candidate_value,
					    &candidate_namespace_pid) ||
		    candidate_namespace_pid != namespace_pid)
			continue;
		*host_pid = (__u32)candidate_value;
		fclose(file);
		return 0;
	}
	fclose(file);
	return -ESRCH;
}

static int register_root_task(__u64 generation)
{
	__u64 timestamp = now_ns();
	__u32 task_flags = 0;
	__u64 blocking_id = 0;
	__u64 blocking_generation = 0;

	if (tool_id) {
		if (root_is_scope_only) {
			/* 执行范围进程只负责产生并等待工具子进程，不计入工具成员。 */
			task_flags = FLOW_V2_TASK_WAITING_CHILD |
				FLOW_V2_TASK_SCOPE_ROOT;
		} else {
			task_flags = FLOW_V2_TASK_TOOL_MEMBER |
				FLOW_V2_TASK_WAIT_TARGET |
				FLOW_V2_TASK_RETURN_ROOT;
			blocking_id = tool_id;
			blocking_generation = generation;
		}
	}
	struct flow_v2_task_meta meta = {
		.session_id = session_id,
		.cgroup_id = cgroup_id,
		.tool_id = tool_id,
		.parent_tool_id = parent_tool_id,
		.last_role_change_ns = timestamp,
		.birth_ns = timestamp,
		.role = role,
		.role_source = explicit_role ? FLOW_V2_SOURCE_TOOL : FLOW_V2_SOURCE_DEFAULT,
		.role_reason = explicit_role ? FLOW_V2_REASON_TOOL_EXECUTION : FLOW_V2_REASON_NONE,
		.stage = stage,
		.behavior = behavior_from_stage(stage),
		.last_cpu = -1,
		.sequence = generation,
		.blocking_tool_id = blocking_id,
		.blocking_sequence = blocking_generation,
		.flags = task_flags,
	};
	if (!root_pid)
		return 0;
	return bpf_map_update_elem(task_fd, &root_pid, &meta, BPF_ANY);
}

static int validate_root_cgroup(void)
{
	char resolved[PATH_MAX];
	__u64 actual_cgroup_id;
	int err;

	if (!root_pid)
		return 0;
	err = cgroup_id_from_pid(root_pid, &actual_cgroup_id, resolved,
				 sizeof(resolved));
	if (err) {
		fprintf(stderr, "无法核对工具根进程 %u 的 cgroup：%s\n",
			root_pid, strerror(-err));
		return err;
	}
	if (actual_cgroup_id != cgroup_id) {
		fprintf(stderr,
			"进程 %u 不属于当前会话 cgroup：实际=%llu，预期=%llu\n",
			root_pid, (unsigned long long)actual_cgroup_id,
			(unsigned long long)cgroup_id);
		return -EXDEV;
	}
	return 0;
}

static void delete_tools_for_session(__u64 target_session)
{
	struct flow_v2_tool_key key, next;
	int err;

	if (active_index_session == target_session)
		active_index_valid = false;

	err = bpf_map_get_next_key(tool_fd, NULL, &key);
	while (!err) {
		int next_err = bpf_map_get_next_key(tool_fd, &key, &next);

		if (key.session_id == target_session)
			bpf_map_delete_elem(tool_fd, &key);
		if (next_err)
			break;
		key = next;
	}
	err = bpf_map_get_next_key(tool_runtime_fd, NULL, &key);
	while (!err) {
		int next_err = bpf_map_get_next_key(tool_runtime_fd, &key, &next);

		if (key.session_id == target_session)
			bpf_map_delete_elem(tool_runtime_fd, &key);
		if (next_err)
			break;
		key = next;
	}
}

static int session_open(void)
{
	struct flow_v2_session_runtime_user runtime = {};
	struct flow_v2_session_runtime_user old_runtime = {};
	struct flow_v2_session_adaptive_state_user adaptive_state = {};
	struct flow_v2_fair_runtime_user fair_runtime = {};
	struct flow_v2_session_config cfg;
	struct flow_v2_session_config old_cfg;
	bool had_old;
	bool had_old_runtime = false;
	bool preserve_runtime = false;
	__u64 current;
	__u64 timestamp;
	int err = resolve_cgroup();
	__u64 cpu_quota_ns = 0;
	__u64 cpu_period_ns = 0;

	if (err) {
		fprintf(stderr, "无法确定会话 cgroup：%s\n", strerror(-err));
		return err;
	}
	session_root_pid = root_pid;
	/* 内核 cgroup 权重是第一层调度的真实约束，不能由事件参数覆盖。 */
	{
		__u32 actual_weight = weight;

		if (!read_cgroup_cpu_weight(&actual_weight))
			weight = actual_weight;
	}
	read_cgroup_cpu_max(&cpu_quota_ns, &cpu_period_ns);
	adaptive_state.effective_weight = weight;
	/*
	 * 同一会话的控制程序异常结束后，新的控制程序先清除失效的工具
	 * 状态和会话队列位置，再建立新一代会话。仍在定期更新时间戳的
	 * 控制程序不会被另一个实例覆盖。
	 */
	had_old = !bpf_map_lookup_elem(session_fd, &cgroup_id, &old_cfg);
	if (had_old)
		had_old_runtime = !bpf_map_lookup_elem(session_runtime_fd,
						     &old_cfg.session_id,
						     &old_runtime);
	current = now_ns();
	if (had_old &&
	    (old_cfg.flags & FLOW_V2_SESSION_LEASED) && old_cfg.controller_pid &&
	    old_cfg.controller_pid != (__u32)getpid() &&
	    current >= old_cfg.heartbeat_ns &&
	    current - old_cfg.heartbeat_ns <= FLOW_V2_HEARTBEAT_TIMEOUT_NS &&
	    (!kill((pid_t)old_cfg.controller_pid, 0) || errno == EPERM)) {
		fprintf(stderr, "会话已有活动控制程序：pid=%u\n",
			old_cfg.controller_pid);
		return -EALREADY;
	}
	/*
	 * 异常接管同一会话时，内核调度器从未停止，正在运行和可运行任务
	 * 的计数仍然有效，不能用零值覆盖。若调用者试图把仍有活动任务
	 * 的 cgroup 改成另一个会话，则拒绝该操作，避免旧任务被记入新
	 * 会话。正常结束旧任务后可以重新登记。
	 */
	preserve_runtime = had_old && old_cfg.session_id == session_id &&
			   had_old_runtime;
	if (preserve_runtime && old_runtime.runnable_tasks &&
	    (old_cfg.weight ?: 1) != weight) {
		fprintf(stderr, "会话仍有可运行任务，不能在接管时改变权重\n");
		return -EBUSY;
	}
	if (had_old && old_cfg.session_id != session_id && had_old_runtime &&
	    (old_runtime.runnable_tasks || old_runtime.running_tasks ||
	     old_runtime.reserved_tasks)) {
		fprintf(stderr, "原会话仍有活动任务，不能更换会话编号\n");
		return -EBUSY;
	}
	bpf_map_delete_elem(active_tool_fd, &cgroup_id);
	if (had_old)
		delete_tools_for_session(old_cfg.session_id);
	if (!had_old || old_cfg.session_id != session_id)
		delete_tools_for_session(session_id);
	if (had_old && !preserve_runtime)
		bpf_map_delete_elem(session_runtime_fd, &old_cfg.session_id);
	if (!had_old || old_cfg.session_id != session_id)
		bpf_map_delete_elem(session_runtime_fd, &session_id);
	if (had_old && !preserve_runtime)
		bpf_map_delete_elem(session_adaptive_state_fd, &old_cfg.session_id);
	if (!had_old || old_cfg.session_id != session_id)
		bpf_map_delete_elem(session_adaptive_state_fd, &session_id);
	if (had_old && !preserve_runtime)
		bpf_map_delete_elem(session_fair_runtime_fd, &old_cfg.session_id);
	if (!had_old || old_cfg.session_id != session_id)
		bpf_map_delete_elem(session_fair_runtime_fd, &session_id);
	release_session_slots_for_cgroup(cgroup_id);
	timestamp = now_ns();
	memset(&cfg, 0, sizeof(cfg));
	cfg.session_id = session_id;
	cfg.cgroup_id = cgroup_id;
	cfg.opened_ns = timestamp;
	cfg.policy_window_ns = policy_window_ms * 1000000ULL;
	cfg.boost_budget_ns = boost_budget_ms * 1000000ULL;
	cfg.progress_bias_ns = progress_bias_us * 1000ULL;
	cfg.interactive_bias_ns = interactive_bias_us * 1000ULL;
	cfg.background_delay_ns = background_delay_us * 1000ULL;
	cfg.cpu_quota_ns = cpu_quota_ns;
	cfg.cpu_period_ns = cpu_period_ns;
	cfg.weight = weight;
	cfg.flags = FLOW_V2_SESSION_ENABLED;
	if (serve_socket_path) {
		cfg.flags |= FLOW_V2_SESSION_LEASED;
		cfg.heartbeat_ns = timestamp;
		cfg.controller_pid = (__u32)getpid();
	}
	cfg.max_progress_group = max_progress_group;
	cfg.max_progress_depth = max_progress_depth;
	if (!preserve_runtime) {
		if (bpf_map_update_elem(session_runtime_fd, &session_id, &runtime,
					BPF_ANY))
			return -errno;
		if (bpf_map_update_elem(session_adaptive_state_fd, &session_id,
					&adaptive_state, BPF_ANY)) {
			err = -errno;
			bpf_map_delete_elem(session_runtime_fd, &session_id);
			return err;
		}
		if (bpf_map_update_elem(session_fair_runtime_fd, &session_id,
					&fair_runtime, BPF_ANY)) {
			err = -errno;
			bpf_map_delete_elem(session_adaptive_state_fd, &session_id);
			bpf_map_delete_elem(session_runtime_fd, &session_id);
			return err;
		}
	}
	if (bpf_map_update_elem(session_fd, &cgroup_id, &cfg, BPF_ANY)) {
		err = -errno;
		goto out_runtime;
	}
	if ((err = claim_session_slot())) {
		bpf_map_delete_elem(session_fd, &cgroup_id);
		return err;
	}
	if ((err = register_root_task(sequence)))
		goto out_slot;
	{
		long long weight_delta = had_old ? (long long)weight -
			(long long)(old_cfg.weight ?: 1) : weight;
		err = adjust_control_state(had_old ? 0 : 1, weight_delta,
					   0, 0, 0, 0);
	}
	if (err)
		goto out_slot;
	printf("flowd registered session=%s session_id=%llu"
	       " cgroup_id=%llu root_pid=%u\n",
	       session_name, (unsigned long long)session_id,
	       (unsigned long long)cgroup_id, root_pid);
	fflush(stdout);
	return 0;

out_slot:
	release_session_slot();
	bpf_map_delete_elem(session_fd, &cgroup_id);
out_runtime:
	bpf_map_delete_elem(session_fair_runtime_fd, &session_id);
	bpf_map_delete_elem(session_adaptive_state_fd, &session_id);
	bpf_map_delete_elem(session_runtime_fd, &session_id);
	return err;
}

static int refresh_session_heartbeat(void)
{
	struct flow_v2_session_config cfg;

	if (!serve_socket_path)
		return 0;
	if (session_root_pid && kill((pid_t)session_root_pid, 0) && errno == ESRCH) {
		fprintf(stderr, "会话根进程 %u 已结束，清理会话状态\n",
			session_root_pid);
		return -ESRCH;
	}
	if (bpf_map_lookup_elem(session_fd, &cgroup_id, &cfg))
		return -errno;
	if (cfg.session_id != session_id ||
	    cfg.controller_pid != (__u32)getpid() ||
	    !(cfg.flags & FLOW_V2_SESSION_LEASED))
		return -ESTALE;
	cfg.heartbeat_ns = now_ns();
	if (bpf_map_update_elem(session_fd, &cgroup_id, &cfg, BPF_EXIST))
		return -errno;
	return 0;
}

static int session_close(void)
{
	struct flow_v2_session_config cfg;
	bool had_cfg;
	int err = resolve_cgroup();

	if (err)
		return err;
	had_cfg = !bpf_map_lookup_elem(session_fd, &cgroup_id, &cfg);
	if (had_cfg &&
	    serve_socket_path && (cfg.flags & FLOW_V2_SESSION_LEASED) &&
	    cfg.controller_pid != (__u32)getpid())
		return 0;
	if (had_cfg) {
		struct flow_v2_session_runtime_user runtime = {};
		struct flow_v2_session_adaptive_state_user adaptive = {};
		bool active = !bpf_map_lookup_elem(session_runtime_fd,
						   &cfg.session_id, &runtime) &&
			      runtime.runnable_tasks;
		unsigned active_demand = runtime.runnable_tasks;
		unsigned active_weight = cfg.weight ?: 1;

		if (!bpf_map_lookup_elem(session_adaptive_state_fd,
					 &cfg.session_id, &adaptive) &&
		    adaptive.effective_weight)
			active_weight = adaptive.effective_weight;
		if (active_weight > 10000)
			active_weight = 10000;

		/* 先停用策略，使随后进入 quiescent 的任务不再重复扣减。 */
		cfg.flags &= ~FLOW_V2_SESSION_ENABLED;
		if (bpf_map_update_elem(session_fd, &cgroup_id, &cfg, BPF_EXIST))
			return -errno;
		err = adjust_control_state(-1, -(long long)(cfg.weight ?: 1),
					 active ? (long long)active_weight : 0,
					 active ? 1 : 0,
					 active ? (int)active_demand : 0,
					 (int)runtime.running_tasks);
		if (err)
			return err;
	}
	if (bpf_map_delete_elem(session_fd, &cgroup_id) && errno != ENOENT)
		return -errno;
	bpf_map_delete_elem(active_tool_fd, &cgroup_id);
	delete_tools_for_session(session_id);
	bpf_map_delete_elem(session_runtime_fd, &session_id);
	bpf_map_delete_elem(session_adaptive_state_fd, &session_id);
	bpf_map_delete_elem(session_fair_runtime_fd, &session_id);
	release_session_slots_for_cgroup(cgroup_id);
	claimed_slot = FLOW_V2_ACTIVE_SESSION_SLOTS;
	printf("flowd closed session=%s session_id=%llu cgroup_id=%llu\n",
	       session_name, (unsigned long long)session_id,
	       (unsigned long long)cgroup_id);
	return 0;
}

static int check_session_sequence(__u64 candidate)
{
	struct flow_v2_session_config cfg;

	if (bpf_map_lookup_elem(session_fd, &cgroup_id, &cfg))
		return -errno;
	if (cfg.session_id != session_id)
		return -ESTALE;
	if (!candidate || candidate <= cfg.last_sequence) {
		fprintf(stderr, "拒绝重复或乱序事件：当前=%llu，收到=%llu\n",
			(unsigned long long)cfg.last_sequence,
			(unsigned long long)candidate);
		return -ESTALE;
	}
	/* 其他控制进程提交过事件时重新恢复，不能沿用旧的活动集合。 */
	if (active_index_session != session_id ||
	    active_index_sequence != cfg.last_sequence)
		active_index_valid = false;
	return 0;
}

static int commit_session_sequence(__u64 accepted)
{
	struct flow_v2_session_config cfg;

	if (bpf_map_lookup_elem(session_fd, &cgroup_id, &cfg))
		return -errno;
	if (cfg.session_id != session_id || accepted <= cfg.last_sequence)
		return -ESTALE;
	cfg.last_sequence = accepted;
	if (bpf_map_update_elem(session_fd, &cgroup_id, &cfg, BPF_EXIST))
		return -errno;
	active_index_sequence = accepted;
	return 0;
}

static int index_active_tool(__u64 id)
{
	for (size_t i = 0; i < nr_active_tools; i++)
		if (active_tool_ids[i] == id)
			return 0;
	if (nr_active_tools == FLOW_V2_MAX_TOOLS)
		return -ENOSPC;
	active_tool_ids[nr_active_tools++] = id;
	return 0;
}

static int restore_active_tool_index(void)
{
	struct flow_v2_tool_key key, next;
	struct flow_v2_tool_config cfg;
	int err;

	nr_active_tools = 0;
	active_index_valid = false;
	err = bpf_map_get_next_key(tool_fd, NULL, &next);
	while (!err) {
		key = next;
		if (key.session_id == session_id) {
			if (!bpf_map_lookup_elem(tool_fd, &key, &cfg)) {
				/* 其他会话删除游标所在条目时，内核可能从头遍历。
				 * 复用插入函数去重，不能把重复遇到的编号重复保存。 */
				if (cfg.active && (err = index_active_tool(key.tool_id)))
					return err;
			} else if (errno != ENOENT) {
				return -errno;
			}
		}
		err = bpf_map_get_next_key(tool_fd, &key, &next);
	}
	if (errno != ENOENT)
		return -errno;
	active_index_session = session_id;
	active_index_valid = true;
	return 0;
}

/*
 * 一个会话可以同时保留多个活动工具。这个指针只表示当前最应作为
 * 前台参照的工具：优先选择同步工具，再选择最近更新的异步工具。
 * 每个工具自己的身份和生命周期仍保存在 tool_config 中。
 */
static int refresh_current_tool(void)
{
	struct flow_v2_tool_config candidate;
	struct flow_v2_tool_config best = {};
	struct flow_v2_tool_key key, best_key = {};
	bool found = false;
	int err;

	if (!active_index_valid || active_index_session != session_id) {
		err = restore_active_tool_index();
		if (err)
			return err;
	}
	/* 本次新开始的工具尚未出现在已有索引中；结束的工具在下面移除。 */
	if (tool_id && (err = index_active_tool(tool_id)))
		return err;
	for (size_t i = 0; i < nr_active_tools;) {
		key = (struct flow_v2_tool_key) {
			.session_id = session_id,
			.tool_id = active_tool_ids[i],
		};
		err = bpf_map_lookup_elem(tool_fd, &key, &candidate);
		if (err && errno != ENOENT)
			return -errno;
		if (err || !candidate.active) {
			active_tool_ids[i] = active_tool_ids[--nr_active_tools];
			continue;
		}
		i++;
		if (!found ||
		     (best.role == FLOW_V2_ROLE_BACKGROUND &&
		      candidate.role != FLOW_V2_ROLE_BACKGROUND) ||
		     ((best.role == FLOW_V2_ROLE_BACKGROUND) ==
		      (candidate.role == FLOW_V2_ROLE_BACKGROUND) &&
		      candidate.updated_ns > best.updated_ns)) {
			best = candidate;
			best_key = key;
			found = true;
		}
	}
	if (!found) {
		if (bpf_map_delete_elem(active_tool_fd, &cgroup_id) && errno != ENOENT)
			return -errno;
		return 0;
	}
	{
		struct flow_v2_active_tool current = {
			.session_id = session_id,
			.cgroup_id = cgroup_id,
			.tool_id = best_key.tool_id,
			.parent_tool_id = best.parent_tool_id,
			.sequence = best.generation,
			.role = best.role,
			.role_source = FLOW_V2_SOURCE_TOOL,
			.stage = best.stage,
			.behavior = behavior_from_stage(best.stage),
			.active = 1,
		};

		if (bpf_map_update_elem(active_tool_fd, &cgroup_id, &current, BPF_ANY))
			return -errno;
	}
	return 0;
}

static int tool_change(enum operation tool_operation)
{
	bool active = tool_operation != OP_TOOL_END;
	bool begin = tool_operation == OP_TOOL_BEGIN;
	struct flow_v2_tool_config old, cfg = {
		.parent_tool_id = parent_tool_id,
		.started_ns = now_ns(),
		.updated_ns = now_ns(),
		.sequence = sequence,
		.generation = tool_generation,
		.role = role,
		.stage = stage,
		.active = active ? 1U : 0U,
		.flags = manual_progress ? FLOW_V2_TOOL_MANUAL_PROGRESS : 0,
	};
	struct flow_v2_tool_key key = {
		.session_id = session_id,
		.tool_id = tool_id,
	};
	struct flow_v2_tool_runtime runtime = {};
	struct flow_v2_tool_runtime old_runtime;
	__u64 timestamp = cfg.updated_ns;
	__u64 generation = tool_generation;
	int err;

	err = resolve_cgroup();
	if (err)
		return err;
	if (!tool_id) {
		fprintf(stderr, "工具操作必须提供 --tool-id\n");
		return -EINVAL;
	}
	if (!tool_generation) {
		fprintf(stderr, "工具操作必须提供非零代次编号\n");
		return -EINVAL;
	}
	err = check_session_sequence(sequence);
	if (err)
		return err;
	if (root_pid && active) {
		int err = validate_root_cgroup();

		if (err)
			return err;
	}
	if (!bpf_map_lookup_elem(tool_fd, &key, &old)) {
		if (sequence <= old.sequence) {
			fprintf(stderr, "拒绝过期工具事件：当前=%llu，收到=%llu\n",
				(unsigned long long)old.sequence,
				(unsigned long long)sequence);
			return -ESTALE;
		}
		if (begin && old.active)
			return -EALREADY;
		if (begin && tool_generation <= old.generation)
			return -ESTALE;
		if (!begin && (!old.active || tool_generation != old.generation))
			return -ESTALE;
		if (!begin)
			cfg.started_ns = old.started_ns;
	} else if (!begin) {
		return -ENOENT;
	}
	if (bpf_map_update_elem(tool_fd, &key, &cfg, BPF_ANY))
		return -errno;
	if (begin) {
		runtime.started_ns = timestamp;
		runtime.root_pid = root_is_scope_only ? 0 : root_pid;
		runtime.sequence = generation;
		/* 执行范围进程不属于工具；第一次 fork 的子进程才是工具根。 */
		/* 成员计数完全由内核在首次看见真实 task_struct 时认领。 */
		runtime.live_tasks = 0;
		runtime.peak_live_tasks = 0;
		runtime.preferred_cpu = -1;
		runtime.active = 1;
		if (bpf_map_update_elem(tool_runtime_fd, &key, &runtime, BPF_ANY))
			return -errno;
	} else if (!bpf_map_lookup_elem(tool_runtime_fd, &key, &old_runtime)) {
		if (active) {
			old_runtime.active = 1;
		} else {
			old_runtime.ended_ns = timestamp;
			old_runtime.active = 0;
		}
		if (bpf_map_update_elem(tool_runtime_fd, &key, &old_runtime, BPF_ANY))
			return -errno;
	}
	if (root_pid && active) {
		err = register_root_task(generation);

		if (err)
			return err;
	}
	err = refresh_current_tool();
	if (err)
		return err;
	err = commit_session_sequence(sequence);
	if (err)
		return err;
	printf("flowd tool session_id=%llu tool_id=%llu"
	       " sequence=%llu role=%u stage=%u active=%u root_pid=%u scope_only=%u\n",
	       (unsigned long long)session_id, (unsigned long long)tool_id,
	       (unsigned long long)sequence, role, stage, active ? 1U : 0U,
	       root_pid, root_is_scope_only ? 1U : 0U);
	fflush(stdout);
	return 0;
}

static int tool_dependency_change(void)
{
	struct flow_v2_tool_config cfg;
	struct flow_v2_tool_key key = {
		.session_id = session_id,
		.tool_id = tool_id,
	};
	int err;

	err = resolve_cgroup();
	if (err)
		return err;
	if (!tool_id || tool_id == parent_tool_id)
		return -EINVAL;
	if (!tool_generation)
		return -EINVAL;
	err = check_session_sequence(sequence);
	if (err)
		return err;
	if (bpf_map_lookup_elem(tool_fd, &key, &cfg))
		return -ENOENT;
	if (!cfg.active || cfg.generation != tool_generation)
		return -ESTALE;
	if (parent_tool_id) {
		struct flow_v2_tool_config parent;
		struct flow_v2_tool_key parent_key = {
			.session_id = session_id,
			.tool_id = parent_tool_id,
		};

		if (bpf_map_lookup_elem(tool_fd, &parent_key, &parent) ||
		    !parent.active)
			return -ENOENT;
	}
	cfg.parent_tool_id = parent_tool_id;
	cfg.updated_ns = now_ns();
	cfg.sequence = sequence;
	if (bpf_map_update_elem(tool_fd, &key, &cfg, BPF_EXIST))
		return -errno;
	err = refresh_current_tool();
	if (err)
		return err;
	err = commit_session_sequence(sequence);
	if (err)
		return err;
	printf("flowd dependency session_id=%llu tool_id=%llu parent_tool_id=%llu sequence=%llu\n",
	       (unsigned long long)session_id, (unsigned long long)tool_id,
	       (unsigned long long)parent_tool_id,
	       (unsigned long long)sequence);
	fflush(stdout);
	return 0;
}

static void forget_client_tool(struct flow_v2_client *clients,
			       __u64 ended_tool_id, __u64 ended_generation)
{
	int index;

	for (index = 0; index < FLOW_V2_MAX_CLIENTS; index++) {
		struct flow_v2_client_tool **link = &clients[index].tools;

		while (*link) {
			struct flow_v2_client_tool *entry = *link;

			if (entry->tool_id == ended_tool_id &&
			    entry->generation == ended_generation) {
				*link = entry->next;
				free(entry);
				continue;
			}
			link = &entry->next;
		}
	}
}

static int end_client_tools_after_disconnect(struct flow_v2_client *client)
{
	int first_err = 0;

	while (client->tools) {
		struct flow_v2_client_tool *entry = client->tools;
		struct flow_v2_session_config session;
		struct flow_v2_tool_config cfg;
		struct flow_v2_tool_key key = {
			.session_id = session_id,
			.tool_id = entry->tool_id,
		};
		int err = 0;

		client->tools = entry->next;
		if (!bpf_map_lookup_elem(tool_fd, &key, &cfg) && cfg.active &&
		    cfg.generation == entry->generation) {
			tool_id = entry->tool_id;
			parent_tool_id = cfg.parent_tool_id;
			tool_generation = cfg.generation;
			sequence = cfg.sequence + 1;
			if (!bpf_map_lookup_elem(session_fd, &cgroup_id, &session) &&
			    sequence <= session.last_sequence)
				sequence = session.last_sequence + 1;
			role = cfg.role;
			stage = cfg.stage;
			explicit_role = true;
			root_pid = 0;
			root_is_scope_only = false;
			printf("flowd connection closed; ending tool_id=%llu generation=%llu\n",
			       (unsigned long long)tool_id,
			       (unsigned long long)tool_generation);
			fflush(stdout);
			err = tool_change(OP_TOOL_END);
		}
		if (err && !first_err)
			first_err = err;
		free(entry);
	}
	return first_err;
}

static void print_session_stats(__u64 sid, const struct flow_v2_session_stats *s)
{
	printf("{\"session_id\":%llu,\"enqueues\":%llu"
	       ",\"runs\":%llu,\"wait_ns\":%llu"
	       ",\"runtime_ns\":%llu,\"bias_ns\":%llu"
	       ",\"budget_limited\":%llu,\"forks\":%llu"
	       ",\"execs\":%llu,\"exits\":%llu,\"tail_promotions\":%llu"
	       ",\"wait_events\":%llu,\"wait_links\":%llu"
	       ",\"cross_generation_wait_links\":%llu"
	       ",\"return_candidates\":%llu,\"return_promotions\":%llu"
	       ",\"return_root_promotions\":%llu"
	       ",\"wait_target_promotions\":%llu,\"wait_any_links\":%llu"
	       ",\"return_rejected_no_wait\":%llu"
	       ",\"return_rejected_not_tail\":%llu"
	       ",\"stale_generation_tasks\":%llu,\"refunded_bias_ns\":%llu"
	       ",\"background_rescues\":%llu"
	       ",\"ignored_nonblocking_waits\":%llu"
	       ",\"confirmed_blocking_waits\":%llu"
	       ",\"ignored_immediate_waits\":%llu"
	       ",\"ignored_group_waits\":%llu"
	       ",\"provenance_tasks\":%llu,\"reused_pid_replacements\":%llu"
	       ",\"progress_group_links\":%llu,\"progress_group_promotions\":%llu"
	       ",\"nested_wait_links\":%llu,\"nested_wait_promotions\":%llu"
	       ",\"max_progress_depth_observed\":%llu"
	       ",\"progress_group_relays\":%llu"
	       ",\"max_progress_relay_depth\":%llu"
	       ",\"first_relation_promotions\":%llu"
	       ",\"sleeping_descendant_candidates\":%llu"
	       ",\"sleeping_descendant_promotions\":%llu"
	       ",\"sleeping_descendant_invalidations\":%llu"
	       ",\"max_sleeping_descendant_depth\":%llu"
	       ",\"sustained_descendant_runs\":%llu"
	       ",\"sustained_descendant_queue_ns\":%llu"
	       ",\"sustained_pressure_events\":%llu"
	       ",\"sustained_pressure_activations\":%llu"
	       ",\"sustained_pressure_cooldowns\":%llu"
	       ",\"descendant_wake_links\":%llu"
	       ",\"descendant_wake_root_links\":%llu"
	       ",\"descendant_wake_queued_links\":%llu"
	       ",\"descendant_waker_queue_ns\":%llu"
	       ",\"descendant_wake_long_links\":%llu"
	       ",\"descendant_wake_pressure_links\":%llu"
	       ",\"descendant_wake_sleep_ns\":%llu"
	       ",\"descendant_wake_max_sleep_ns\":%llu"
	       ",\"descendant_waker_marks\":%llu"
	       ",\"descendant_waker_requeues\":%llu"
	       ",\"descendant_root_waker_marks\":%llu"
	       ",\"descendant_root_waker_requeues\":%llu"
	       ",\"descendant_waker_promotions\":%llu"
	       ",\"descendant_waker_promotion_runs\":%llu"
	       ",\"descendant_root_waker_promotions\":%llu"
	       ",\"stale_delays\":%llu,\"locality_hits\":%llu"
	       ",\"locality_fallbacks\":%llu"
	       ",\"interactive_wakeups\":%llu"
	       ",\"interactive_preemptions\":%llu"
	       ",\"interactive_runs\":%llu"
	       ",\"exec_reclassifications\":%llu"
	       ",\"fair_dispatch_rejections\":%llu"
	       ",\"fair_dispatch_borrows\":%llu"
	       ",\"session_width_limits\":%llu"
	       ",\"session_width_borrows\":%llu"
	       ",\"adaptive_weight_activations\":%llu"
	       ",\"adaptive_weight_deactivations\":%llu"
	       ",\"adaptive_weight_active_ns\":%llu"
	       ",\"adaptive_weight_dispatches\":%llu"
	       ",\"adaptive_weight_changes\":%llu"
	       ",\"adaptive_weight_peak\":%llu"
	       ",\"automatic_weight_timeouts\":%llu"
	       ",\"automatic_weight_condition_releases\":%llu"
	       ",\"automatic_weight_wait_returns\":%llu"
	       ",\"automatic_weight_handoffs\":%llu"
	       ",\"automatic_weight_owner_change_releases\":%llu"
	       ",\"automatic_weight_pressure_waits\":%llu"
	       ",\"automatic_weight_max_active_ns\":%llu"
	       ",\"automatic_weight_service_releases\":%llu"
	       ",\"automatic_weight_planned_ns\":%llu"
	       ",\"automatic_weight_service_ns\":%llu"
	       ",\"quota_throttles\":%llu"
	       ",\"quota_runtime_ns\":%llu"
	       ",\"quota_dispatch_rejections\":%llu"
	       ",\"quota_direct_rejections\":%llu"
	       ",\"parallel_width_limits\":%llu"
	       ",\"parallel_width_borrows\":%llu"
	       ",\"role_enqueues\":[%llu,%llu,%llu,%llu,%llu]"
	       ",\"role_runtime_ns\":[%llu,%llu,%llu,%llu,%llu]}\n",
	       (unsigned long long)sid, (unsigned long long)s->enqueues,
	       (unsigned long long)s->runs, (unsigned long long)s->runnable_wait_ns,
	       (unsigned long long)s->runtime_ns, (unsigned long long)s->applied_bias_ns,
	       (unsigned long long)s->budget_limited, (unsigned long long)s->forks,
	       (unsigned long long)s->execs, (unsigned long long)s->exits,
	       (unsigned long long)s->tail_promotions,
	       (unsigned long long)s->wait_events,
	       (unsigned long long)s->wait_links,
	       (unsigned long long)s->cross_generation_wait_links,
	       (unsigned long long)s->return_candidates,
	       (unsigned long long)s->return_promotions,
	       (unsigned long long)s->return_root_promotions,
	       (unsigned long long)s->wait_target_promotions,
	       (unsigned long long)s->wait_any_links,
	       (unsigned long long)s->return_rejected_no_wait,
	       (unsigned long long)s->return_rejected_not_tail,
	       (unsigned long long)s->stale_generation_tasks,
	       (unsigned long long)s->refunded_bias_ns,
	       (unsigned long long)s->background_rescues,
	       (unsigned long long)s->ignored_nonblocking_waits,
	       (unsigned long long)s->confirmed_blocking_waits,
	       (unsigned long long)s->ignored_immediate_waits,
	       (unsigned long long)s->ignored_group_waits,
	       (unsigned long long)s->provenance_tasks,
	       (unsigned long long)s->reused_pid_replacements,
	       (unsigned long long)s->progress_group_links,
	       (unsigned long long)s->progress_group_promotions,
	       (unsigned long long)s->nested_wait_links,
	       (unsigned long long)s->nested_wait_promotions,
	       (unsigned long long)s->max_progress_depth_observed,
	       (unsigned long long)s->progress_group_relays,
	       (unsigned long long)s->max_progress_relay_depth,
	       (unsigned long long)s->first_relation_promotions,
	       (unsigned long long)s->sleeping_descendant_candidates,
	       (unsigned long long)s->sleeping_descendant_promotions,
	       (unsigned long long)s->sleeping_descendant_invalidations,
	       (unsigned long long)s->max_sleeping_descendant_depth,
	       (unsigned long long)s->sustained_descendant_runs,
	       (unsigned long long)s->sustained_descendant_queue_ns,
	       (unsigned long long)s->sustained_pressure_events,
	       (unsigned long long)s->sustained_pressure_activations,
	       (unsigned long long)s->sustained_pressure_cooldowns,
	       (unsigned long long)s->descendant_wake_links,
	       (unsigned long long)s->descendant_wake_root_links,
	       (unsigned long long)s->descendant_wake_queued_links,
	       (unsigned long long)s->descendant_waker_queue_ns,
	       (unsigned long long)s->descendant_wake_long_links,
	       (unsigned long long)s->descendant_wake_pressure_links,
	       (unsigned long long)s->descendant_wake_sleep_ns,
	       (unsigned long long)s->descendant_wake_max_sleep_ns,
	       (unsigned long long)s->descendant_waker_marks,
	       (unsigned long long)s->descendant_waker_requeues,
	       (unsigned long long)s->descendant_root_waker_marks,
	       (unsigned long long)s->descendant_root_waker_requeues,
	       (unsigned long long)s->descendant_waker_promotions,
	       (unsigned long long)s->descendant_waker_promotion_runs,
	       (unsigned long long)s->descendant_root_waker_promotions,
	       (unsigned long long)s->stale_delays,
	       (unsigned long long)s->locality_hits,
	       (unsigned long long)s->locality_fallbacks,
	       (unsigned long long)s->interactive_wakeups,
	       (unsigned long long)s->interactive_preemptions,
	       (unsigned long long)s->interactive_runs,
	       (unsigned long long)s->exec_reclassifications,
	       (unsigned long long)s->fair_dispatch_rejections,
	       (unsigned long long)s->fair_dispatch_borrows,
	       (unsigned long long)s->session_width_limits,
	       (unsigned long long)s->session_width_borrows,
	       (unsigned long long)s->adaptive_weight_activations,
	       (unsigned long long)s->adaptive_weight_deactivations,
	       (unsigned long long)s->adaptive_weight_active_ns,
	       (unsigned long long)s->adaptive_weight_dispatches,
	       (unsigned long long)s->adaptive_weight_changes,
	       (unsigned long long)s->adaptive_weight_peak,
	       (unsigned long long)s->automatic_weight_timeouts,
	       (unsigned long long)s->automatic_weight_condition_releases,
	       (unsigned long long)s->automatic_weight_wait_returns,
	       (unsigned long long)s->automatic_weight_handoffs,
	       (unsigned long long)s->automatic_weight_owner_change_releases,
	       (unsigned long long)s->automatic_weight_pressure_waits,
	       (unsigned long long)s->automatic_weight_max_active_ns,
	       (unsigned long long)s->automatic_weight_service_releases,
	       (unsigned long long)s->automatic_weight_planned_ns,
	       (unsigned long long)s->automatic_weight_service_ns,
	       (unsigned long long)s->quota_throttles,
	       (unsigned long long)s->quota_runtime_ns,
	       (unsigned long long)s->quota_dispatch_rejections,
	       (unsigned long long)s->quota_direct_rejections,
	       (unsigned long long)s->parallel_width_limits,
	       (unsigned long long)s->parallel_width_borrows,
	       (unsigned long long)s->role_enqueues[0],
	       (unsigned long long)s->role_enqueues[1],
	       (unsigned long long)s->role_enqueues[2],
	       (unsigned long long)s->role_enqueues[3],
	       (unsigned long long)s->role_enqueues[4],
	       (unsigned long long)s->role_runtime_ns[0],
	       (unsigned long long)s->role_runtime_ns[1],
	       (unsigned long long)s->role_runtime_ns[2],
	       (unsigned long long)s->role_runtime_ns[3],
	       (unsigned long long)s->role_runtime_ns[4]);
}

static int lookup_session_stats(__u64 sid, struct flow_v2_session_stats *sum)
{
	struct flow_v2_session_stats *percpu;
	size_t value_size = (sizeof(*percpu) + 7U) & ~7U;
	int nr_cpus = libbpf_num_possible_cpus();

	if (nr_cpus <= 0)
		return -EINVAL;
	percpu = calloc((size_t)nr_cpus, value_size);
	if (!percpu)
		return -ENOMEM;
	if (bpf_map_lookup_elem(stats_fd, &sid, percpu)) {
		int err = -errno;

		free(percpu);
		return err;
	}
	memset(sum, 0, sizeof(*sum));
	for (int cpu = 0; cpu < nr_cpus; cpu++) {
		const struct flow_v2_session_stats *src =
			(const void *)((const char *)percpu + (size_t)cpu * value_size);

		sum->enqueues += src->enqueues;
		sum->runs += src->runs;
		sum->runnable_wait_ns += src->runnable_wait_ns;
		sum->runtime_ns += src->runtime_ns;
		sum->applied_bias_ns += src->applied_bias_ns;
		sum->budget_limited += src->budget_limited;
		sum->forks += src->forks;
		sum->execs += src->execs;
		sum->exits += src->exits;
		sum->tail_promotions += src->tail_promotions;
		sum->wait_events += src->wait_events;
		sum->wait_links += src->wait_links;
		sum->cross_generation_wait_links += src->cross_generation_wait_links;
		sum->return_candidates += src->return_candidates;
		sum->return_promotions += src->return_promotions;
		sum->return_root_promotions += src->return_root_promotions;
		sum->wait_target_promotions += src->wait_target_promotions;
		sum->wait_any_links += src->wait_any_links;
		sum->return_rejected_no_wait += src->return_rejected_no_wait;
		sum->return_rejected_not_tail += src->return_rejected_not_tail;
		sum->stale_generation_tasks += src->stale_generation_tasks;
		sum->refunded_bias_ns += src->refunded_bias_ns;
		sum->background_rescues += src->background_rescues;
		sum->ignored_nonblocking_waits += src->ignored_nonblocking_waits;
		sum->confirmed_blocking_waits += src->confirmed_blocking_waits;
		sum->ignored_immediate_waits += src->ignored_immediate_waits;
		sum->ignored_group_waits += src->ignored_group_waits;
		sum->provenance_tasks += src->provenance_tasks;
		sum->reused_pid_replacements += src->reused_pid_replacements;
		sum->progress_group_links += src->progress_group_links;
		sum->progress_group_promotions += src->progress_group_promotions;
		sum->nested_wait_links += src->nested_wait_links;
		sum->nested_wait_promotions += src->nested_wait_promotions;
		if (src->max_progress_depth_observed >
		    sum->max_progress_depth_observed)
			sum->max_progress_depth_observed =
				src->max_progress_depth_observed;
		sum->progress_group_relays += src->progress_group_relays;
		if (src->max_progress_relay_depth >
		    sum->max_progress_relay_depth)
			sum->max_progress_relay_depth =
				src->max_progress_relay_depth;
		sum->first_relation_promotions += src->first_relation_promotions;
		sum->sleeping_descendant_candidates +=
			src->sleeping_descendant_candidates;
		sum->sleeping_descendant_promotions +=
			src->sleeping_descendant_promotions;
		sum->sleeping_descendant_invalidations +=
			src->sleeping_descendant_invalidations;
		if (src->max_sleeping_descendant_depth >
		    sum->max_sleeping_descendant_depth)
			sum->max_sleeping_descendant_depth =
				src->max_sleeping_descendant_depth;
		sum->sustained_descendant_runs += src->sustained_descendant_runs;
		sum->sustained_descendant_queue_ns +=
			src->sustained_descendant_queue_ns;
		sum->sustained_pressure_events += src->sustained_pressure_events;
		sum->sustained_pressure_activations +=
			src->sustained_pressure_activations;
		sum->sustained_pressure_cooldowns +=
			src->sustained_pressure_cooldowns;
		sum->descendant_wake_links += src->descendant_wake_links;
		sum->descendant_wake_root_links += src->descendant_wake_root_links;
		sum->descendant_wake_queued_links +=
			src->descendant_wake_queued_links;
		sum->descendant_waker_queue_ns +=
			src->descendant_waker_queue_ns;
		sum->descendant_wake_long_links += src->descendant_wake_long_links;
		sum->descendant_wake_pressure_links +=
			src->descendant_wake_pressure_links;
		sum->descendant_wake_sleep_ns += src->descendant_wake_sleep_ns;
		if (src->descendant_wake_max_sleep_ns >
		    sum->descendant_wake_max_sleep_ns)
			sum->descendant_wake_max_sleep_ns =
				src->descendant_wake_max_sleep_ns;
		sum->descendant_waker_marks += src->descendant_waker_marks;
		sum->descendant_waker_requeues += src->descendant_waker_requeues;
		sum->descendant_root_waker_marks +=
			src->descendant_root_waker_marks;
		sum->descendant_root_waker_requeues +=
			src->descendant_root_waker_requeues;
		sum->descendant_waker_promotions += src->descendant_waker_promotions;
		sum->descendant_waker_promotion_runs +=
			src->descendant_waker_promotion_runs;
		sum->descendant_root_waker_promotions +=
			src->descendant_root_waker_promotions;
		sum->stale_delays += src->stale_delays;
		sum->locality_hits += src->locality_hits;
		sum->locality_fallbacks += src->locality_fallbacks;
		sum->interactive_wakeups += src->interactive_wakeups;
		sum->interactive_preemptions += src->interactive_preemptions;
		sum->interactive_runs += src->interactive_runs;
		sum->exec_reclassifications += src->exec_reclassifications;
		sum->fair_dispatch_rejections += src->fair_dispatch_rejections;
		sum->fair_dispatch_borrows += src->fair_dispatch_borrows;
		sum->session_width_limits += src->session_width_limits;
		sum->session_width_borrows += src->session_width_borrows;
		sum->adaptive_weight_activations += src->adaptive_weight_activations;
		sum->adaptive_weight_deactivations += src->adaptive_weight_deactivations;
		sum->adaptive_weight_active_ns += src->adaptive_weight_active_ns;
		sum->adaptive_weight_dispatches += src->adaptive_weight_dispatches;
		sum->adaptive_weight_changes += src->adaptive_weight_changes;
		if (src->adaptive_weight_peak > sum->adaptive_weight_peak)
			sum->adaptive_weight_peak = src->adaptive_weight_peak;
		sum->automatic_weight_timeouts += src->automatic_weight_timeouts;
		sum->automatic_weight_condition_releases +=
			src->automatic_weight_condition_releases;
		sum->automatic_weight_wait_returns +=
			src->automatic_weight_wait_returns;
		sum->automatic_weight_handoffs += src->automatic_weight_handoffs;
		sum->automatic_weight_owner_change_releases +=
			src->automatic_weight_owner_change_releases;
		sum->automatic_weight_pressure_waits +=
			src->automatic_weight_pressure_waits;
		if (src->automatic_weight_max_active_ns >
		    sum->automatic_weight_max_active_ns)
			sum->automatic_weight_max_active_ns =
				src->automatic_weight_max_active_ns;
		sum->automatic_weight_service_releases +=
			src->automatic_weight_service_releases;
		sum->automatic_weight_planned_ns +=
			src->automatic_weight_planned_ns;
		sum->automatic_weight_service_ns +=
			src->automatic_weight_service_ns;
		sum->quota_throttles += src->quota_throttles;
		sum->quota_runtime_ns += src->quota_runtime_ns;
		sum->quota_dispatch_rejections += src->quota_dispatch_rejections;
		sum->quota_direct_rejections += src->quota_direct_rejections;
		sum->parallel_width_limits += src->parallel_width_limits;
		sum->parallel_width_borrows += src->parallel_width_borrows;
		for (int role_index = 0; role_index < FLOW_V2_NR_ROLES; role_index++) {
			sum->role_enqueues[role_index] += src->role_enqueues[role_index];
			sum->role_runtime_ns[role_index] += src->role_runtime_ns[role_index];
		}
	}
	free(percpu);
	return 0;
}

static int dump_stats(void)
{
	struct flow_v2_session_stats stats;
	__u64 key, next;
	int err;

	if (session_id) {
		/* 只保留工作流分组的消融运行不会产生策略计数，
		 * 这是合法的全零统计，不是会话丢失。 */
		if (lookup_session_stats(session_id, &stats))
			__builtin_memset(&stats, 0, sizeof(stats));
		print_session_stats(session_id, &stats);
		return 0;
	}
	err = bpf_map_get_next_key(stats_fd, NULL, &next);
	while (!err) {
		key = next;
		if (!lookup_session_stats(key, &stats))
			print_session_stats(key, &stats);
		err = bpf_map_get_next_key(stats_fd, &key, &next);
	}
	return 0;
}

static int handle_socket_request(struct flow_v2_client *client,
				 struct flow_v2_client *clients, char *line)
{
	char operation_text[24] = {};
	char execution_mode_text[24] = {};
	char stage_text[24] = {};
	char peer_cgroup_path[PATH_MAX];
	unsigned long long request_session_id;
	unsigned long long request_tool_id;
	unsigned long long request_parent_id;
	unsigned long long request_generation;
	unsigned long long request_sequence;
	unsigned int protocol_version;
	unsigned int request_root_pid;
	unsigned int request_manual = 0;
	__u64 peer_cgroup_id = 0;
	__u32 request_role;
	__u32 request_stage;
	enum operation request_operation;
	int fields;
	int err;
	struct flow_v2_client_tool *owner = NULL;

	fields = sscanf(line, "%u %23s %llu %llu %llu %llu %llu %23s %23s %u %u",
			&protocol_version, operation_text, &request_session_id,
			&request_tool_id, &request_parent_id, &request_generation,
			&request_sequence,
			execution_mode_text, stage_text, &request_root_pid,
			&request_manual);
	if ((fields != 10 && fields != 11) || request_manual > 1 ||
	    protocol_version != FLOW_V2_PROTOCOL_VERSION) {
		dprintf(client->fd, "ERR %d\n", EPROTONOSUPPORT);
		return -EPROTONOSUPPORT;
	}
	if ((__u64)request_session_id != session_id) {
		dprintf(client->fd, "ERR %d\n", EACCES);
		return -EACCES;
	}
	err = cgroup_id_from_pid(client->peer_pid, &peer_cgroup_id, peer_cgroup_path,
				 sizeof(peer_cgroup_path));
	if (err || peer_cgroup_id != cgroup_id) {
		dprintf(client->fd, "ERR %d\n", EACCES);
		return -EACCES;
	}
	if (
	    (strcmp(operation_text, "tool-begin") &&
	     strcmp(operation_text, "tool-update") &&
	     strcmp(operation_text, "tool-end") &&
	     strcmp(operation_text, "tool-dependency")) ||
	    parse_tool_execution_mode(execution_mode_text, &request_role) ||
	    parse_tool_stage(stage_text, &request_stage)) {
		dprintf(client->fd, "ERR %d\n", EINVAL);
		return -EINVAL;
	}
	tool_id = (__u64)request_tool_id;
	parent_tool_id = (__u64)request_parent_id;
	tool_generation = (__u64)request_generation;
	sequence = (__u64)request_sequence;
	stage = request_stage;
	if (!strcmp(operation_text, "tool-begin"))
		request_operation = OP_TOOL_BEGIN;
	else if (!strcmp(operation_text, "tool-update"))
		request_operation = OP_TOOL_UPDATE;
	else if (!strcmp(operation_text, "tool-dependency"))
		request_operation = OP_TOOL_DEPENDENCY;
	else
		request_operation = OP_TOOL_END;
	if (request_operation == OP_TOOL_BEGIN) {
		owner = calloc(1, sizeof(*owner));
		if (!owner) {
			dprintf(client->fd, "ERR %d\n", ENOMEM);
			return -ENOMEM;
		}
		owner->tool_id = (__u64)request_tool_id;
		owner->generation = (__u64)request_generation;
	}
	/*
	 * 回放进程可能位于容器进程命名空间。工具开始时发送 0，控制程序
	 * 使用套接字对端的宿主机进程号登记执行范围；命令建立后的更新事件
	 * 再把容器进程号转换为宿主机进程号并登记真实工具根。调度角色仍由
	 * 内核等待关系决定，而不是由控制程序指定。
	 */
	root_pid = 0;
	root_is_scope_only = false;
	if (request_operation != OP_TOOL_END &&
	    request_operation != OP_TOOL_DEPENDENCY) {
		if (request_root_pid &&
			!resolve_namespace_pid(client->peer_pid, (__u32)request_root_pid,
					   &root_pid)) {
			root_is_scope_only = false;
		} else {
			root_pid = client->peer_pid;
			root_is_scope_only = true;
		}
	}
	role = request_role;
	explicit_role = true;
	manual_progress = request_manual != 0;
	err = request_operation == OP_TOOL_DEPENDENCY ?
		tool_dependency_change() : tool_change(request_operation);
	if (err) {
		free(owner);
		dprintf(client->fd, "ERR %d\n", -err);
	} else {
		if (request_operation == OP_TOOL_BEGIN) {
			owner->next = client->tools;
			client->tools = owner;
		} else if (request_operation == OP_TOOL_END) {
			forget_client_tool(clients, (__u64)request_tool_id,
					   (__u64)request_generation);
		}
		dprintf(client->fd, "OK\n");
	}
	return err;
}

static int close_client(struct flow_v2_client *client)
{
	int err = end_client_tools_after_disconnect(client);

	if (client->fd >= 0)
		close(client->fd);
	client->fd = -1;
	client->peer_pid = 0;
	client->used = 0;
	return err;
}

static int serve_socket(void)
{
	struct sockaddr_un address = {.sun_family = AF_UNIX};
	struct ring_buffer *ring = NULL;
	struct pollfd pollfds[2 + FLOW_V2_MAX_CLIENTS] = {};
	struct flow_v2_client clients[FLOW_V2_MAX_CLIENTS] = {};
	int server;
	int err = 0;
	int index;

	if (!serve_socket_path ||
	    snprintf(address.sun_path, sizeof(address.sun_path), "%s", serve_socket_path) >=
		(int)sizeof(address.sun_path))
		return -ENAMETOOLONG;
	unlink(serve_socket_path);
	server = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (server < 0)
		return -errno;
	if (bind(server, (struct sockaddr *)&address, sizeof(address))) {
		err = -errno;
		goto out;
	}
	if (root_pid) {
		char proc_path[64];
		struct stat owner;

		if (snprintf(proc_path, sizeof(proc_path), "/proc/%u", root_pid) >=
		    (int)sizeof(proc_path) || stat(proc_path, &owner) ||
		    chown(serve_socket_path, owner.st_uid, owner.st_gid)) {
			err = -errno;
			goto out;
		}
	}
	if (chmod(serve_socket_path, 0600) || listen(server, 32)) {
		err = -errno;
		goto out;
	}
	printf("flowd serving socket=%s\n", serve_socket_path);
	fflush(stdout);
	pollfds[0].fd = server;
	pollfds[0].events = POLLIN;
	pollfds[1].fd = -1;
	for (index = 0; index < FLOW_V2_MAX_CLIENTS; index++) {
		clients[index].fd = -1;
		pollfds[2 + index].fd = -1;
	}
	if (print_events) {
		ring = ring_buffer__new(events_fd, event_callback, NULL, NULL);
		if (!ring) {
			err = errno ? -errno : -ENOMEM;
			fprintf(stderr, "无法建立事件读取器：%s\n", strerror(-err));
			goto out;
		}
		pollfds[1].fd = ring_buffer__epoll_fd(ring);
		pollfds[1].events = POLLIN;
	}
	while (!exit_req) {
		int ready;

		err = refresh_session_heartbeat();
		if (err)
			break;
		ready = poll(pollfds, 2 + FLOW_V2_MAX_CLIENTS, 250);

		if (ready < 0) {
			if (errno == EINTR)
				continue;
			err = -errno;
			break;
		}
		if (!ready)
			continue;
		if (pollfds[1].fd >= 0 && pollfds[1].revents & POLLIN) {
			int consumed = ring_buffer__consume(ring);

			if (consumed < 0) {
				err = consumed;
				break;
			}
		}
		if (pollfds[0].revents & POLLIN) {
			int accepted = accept4(server, NULL, NULL, SOCK_CLOEXEC);
			struct ucred peer = {};
			socklen_t peer_size = sizeof(peer);
			int free_index = -1;

			if (accepted < 0) {
				if (errno == EINTR)
					continue;
				err = -errno;
				break;
			}
			for (index = 0; index < FLOW_V2_MAX_CLIENTS; index++) {
				if (clients[index].fd < 0) {
					free_index = index;
					break;
				}
			}
			if (free_index < 0 ||
			    getsockopt(accepted, SOL_SOCKET, SO_PEERCRED, &peer,
				       &peer_size)) {
				dprintf(accepted, "ERR %d\n",
					free_index < 0 ? EBUSY : errno);
				close(accepted);
			} else {
				clients[free_index].fd = accepted;
				clients[free_index].peer_pid = (__u32)peer.pid;
				clients[free_index].used = 0;
				clients[free_index].tools = NULL;
				pollfds[2 + free_index].fd = accepted;
				pollfds[2 + free_index].events = POLLIN;
			}
		}
		for (index = 0; index < FLOW_V2_MAX_CLIENTS; index++) {
			struct flow_v2_client *client = &clients[index];
			struct pollfd *client_poll = &pollfds[2 + index];
			ssize_t count;

			if (client->fd < 0 ||
			    !(client_poll->revents & (POLLIN | POLLHUP | POLLERR)))
				continue;
			count = read(client->fd, client->buffer + client->used,
				     sizeof(client->buffer) - client->used - 1);
			if (count <= 0) {
				int close_err = close_client(client);

				if (close_err)
					fprintf(stderr,
						"连接中断后的工具清理失败：%s\n",
						strerror(-close_err));
				client_poll->fd = -1;
				continue;
			}
			client->used += (size_t)count;
			client->buffer[client->used] = '\0';
			{
				char *line_start = client->buffer;
				char *newline;

				while ((newline = strchr(line_start, '\n'))) {
					*newline = '\0';
					handle_socket_request(client, clients, line_start);
					line_start = newline + 1;
				}
				client->used -= (size_t)(line_start - client->buffer);
				memmove(client->buffer, line_start, client->used);
			}
			if (client->used == sizeof(client->buffer) - 1) {
				dprintf(client->fd, "ERR %d\n", EMSGSIZE);
				{
					int close_err = close_client(client);

					if (close_err)
						fprintf(stderr,
							"超长消息连接清理失败：%s\n",
							strerror(-close_err));
				}
				client_poll->fd = -1;
			}
		}
	}
out:
	for (index = 0; index < FLOW_V2_MAX_CLIENTS; index++) {
		if (clients[index].fd >= 0)
			close_client(&clients[index]);
	}
	ring_buffer__free(ring);
	close(server);
	unlink(serve_socket_path);
	return err;
}

static int event_callback(void *ctx, void *data, size_t size)
{
	const struct flow_v2_event *event = data;

	(void)ctx;
	if (size < sizeof(*event))
		return 0;
	printf("{\"time_ns\":%llu,\"kind\":%u,\"session_id\":%llu"
	       ",\"cgroup_id\":%llu,\"tool_id\":%llu"
	       ",\"sequence\":%llu,\"pid\":%u,\"related_pid\":%u"
	       ",\"parent_pid\":%u"
	       ",\"role\":%u,\"source\":%u,\"reason\":%u"
	       ",\"stage\":%u,\"behavior\":%u"
	       ",\"tool_live_tasks\":%u,\"tool_peak_live_tasks\":%u"
	       ",\"tool_active_wait_targets\":%u"
	       ",\"tool_peak_wait_targets\":%u"
	       ",\"task_flags\":%u,"
	       "\"comm\":\"%.*s\"}\n",
	       (unsigned long long)event->timestamp_ns, event->kind,
	       (unsigned long long)event->session_id,
	       (unsigned long long)event->cgroup_id,
	       (unsigned long long)event->tool_id,
	       (unsigned long long)event->sequence,
	       event->pid, event->related_pid, event->parent_pid, event->role,
	       event->role_source, event->role_reason, event->stage,
	       event->behavior,
	       event->tool_live_tasks, event->tool_peak_live_tasks,
	       event->tool_active_wait_targets, event->tool_peak_wait_targets,
	       event->task_flags, FLOW_V2_COMM_LEN, event->comm);
	fflush(stdout);
	return 0;
}

int main(int argc, char **argv)
{
	struct ring_buffer *ring = NULL;
	int err;

	process_started_ns = now_ns();
	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);
	if (parse_args(argc, argv))
		return 2;
	if (resource_output_path && atexit(write_resource_usage))
		return 2;
	if ((err = open_maps()))
		return 1;

	switch (operation) {
	case OP_SESSION_OPEN:
		err = session_open();
		if (!err && serve_socket_path) {
			err = serve_socket();
			/* 根进程结束是会话生命周期的正常终点。 */
			if (err == -ESRCH)
				err = 0;
			if (session_close() && !err)
				err = -EIO;
		}
		break;
	case OP_SESSION_CLOSE:
		err = session_close();
		break;
	case OP_TOOL_BEGIN:
	case OP_TOOL_UPDATE:
		err = tool_change(operation);
		break;
	case OP_TOOL_END:
		err = tool_change(operation);
		break;
	case OP_TOOL_DEPENDENCY:
		err = tool_dependency_change();
		break;
	case OP_STATS:
		err = dump_stats();
		break;
	default:
		err = -EINVAL;
	}
	if (err || once || !print_events)
		if (err)
			fprintf(stderr, "flowd 操作失败：%s (%d)\n",
				strerror(-err), -err);
	if (err || once || !print_events)
		return err ? 1 : 0;

	ring = ring_buffer__new(events_fd, event_callback, NULL, NULL);
	if (!ring) {
		fprintf(stderr, "无法建立事件读取器：%s\n", strerror(errno));
		return 1;
	}
	while (!exit_req) {
		err = ring_buffer__poll(ring, 250);
		if (err == -EINTR)
			continue;
		if (err < 0)
			break;
	}
	ring_buffer__free(ring);
	return err < 0 ? 1 : 0;
}
