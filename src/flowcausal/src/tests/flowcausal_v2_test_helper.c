/* SPDX-License-Identifier: GPL-2.0 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <flowcausal_v2.h>

static int open_map(const char *pin_dir, const char *name)
{
	char path[4096];

	if (snprintf(path, sizeof(path), "%s/%s", pin_dir, name) >=
	    (int)sizeof(path))
		return -ENAMETOOLONG;
	return bpf_obj_get(path);
}

static uint64_t number(const char *text)
{
	char *end = NULL;
	unsigned long long value;

	errno = 0;
	value = strtoull(text, &end, 0);
	if (errno || !end || *end) {
		fprintf(stderr, "无效数字：%s\n", text);
		exit(2);
	}
	return value;
}

static uint64_t monotonic_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static int inject_stale_task(const char *pin_dir, uint32_t pid,
			     uint64_t session_id, uint64_t cgroup_id,
			     uint64_t tool_id, uint64_t sequence)
{
	struct flow_v2_task_meta meta = {
		.session_id = session_id,
		.cgroup_id = cgroup_id,
		.tool_id = tool_id,
		.sequence = sequence,
		.task_start_ns = 1,
		.last_cpu = -1,
		.role = FLOW_V2_ROLE_NORMAL,
		.flags = FLOW_V2_TASK_TOOL_MEMBER,
	};
	int fd = open_map(pin_dir, "flow_v2_task_meta");
	int err;

	if (fd < 0)
		return fd;
	err = bpf_map_update_elem(fd, &pid, &meta, BPF_ANY) ? -errno : 0;
	close(fd);
	return err;
}

static int assert_tool_ended(const char *pin_dir, uint64_t session_id,
			     uint64_t cgroup_id, uint64_t tool_id)
{
	struct flow_v2_active_tool active;
	struct flow_v2_tool_runtime runtime;
	struct flow_v2_tool_config cfg;
	struct flow_v2_tool_key key = {
		.session_id = session_id,
		.tool_id = tool_id,
	};
	int active_fd = open_map(pin_dir, "active_tool_by_cgroup");
	int tool_fd = open_map(pin_dir, "tool_config");
	int runtime_fd = open_map(pin_dir, "tool_runtime");
	int err = 0;

	if (active_fd < 0 || tool_fd < 0 || runtime_fd < 0) {
		err = -ENOENT;
		goto out;
	}
	if (!bpf_map_lookup_elem(active_fd, &cgroup_id, &active)) {
		fprintf(stderr, "工具结束后仍保留活动工具\n");
		err = -EBUSY;
		goto out;
	}
	if (errno != ENOENT || bpf_map_lookup_elem(tool_fd, &key, &cfg) ||
	    cfg.active || bpf_map_lookup_elem(runtime_fd, &key, &runtime) ||
	    runtime.active) {
		fprintf(stderr, "工具结束状态不完整\n");
		err = -EINVAL;
	}
out:
	if (active_fd >= 0)
		close(active_fd);
	if (tool_fd >= 0)
		close(tool_fd);
	if (runtime_fd >= 0)
		close(runtime_fd);
	return err;
}

static bool map_has_tool_session(int fd, uint64_t session_id)
{
	struct flow_v2_tool_key key, next;
	int err = bpf_map_get_next_key(fd, NULL, &next);

	while (!err) {
		key = next;
		if (key.session_id == session_id)
			return true;
		err = bpf_map_get_next_key(fd, &key, &next);
	}
	return false;
}

static bool slots_have_cgroup(int fd, uint64_t cgroup_id)
{
	uint32_t key, next;
	int err = bpf_map_get_next_key(fd, NULL, &next);

	while (!err) {
		uint64_t value;

		key = next;
		if (!bpf_map_lookup_elem(fd, &key, &value) && value == cgroup_id)
			return true;
		err = bpf_map_get_next_key(fd, &key, &next);
	}
	return false;
}

static int assert_tool_state(const char *pin_dir, uint64_t session_id,
			     uint64_t cgroup_id, uint64_t expected_count,
			     uint64_t expected_current, uint64_t tool_id,
			     uint64_t parent_tool_id, uint64_t generation,
			     uint32_t stage)
{
	struct flow_v2_tool_key key, next;
	struct flow_v2_tool_config cfg;
	struct flow_v2_active_tool current;
	uint64_t count = 0;
	int active_fd = open_map(pin_dir, "active_tool_by_cgroup");
	int tool_fd = open_map(pin_dir, "tool_config");
	int err;

	if (active_fd < 0 || tool_fd < 0) {
		err = -ENOENT;
		goto out;
	}
	err = bpf_map_get_next_key(tool_fd, NULL, &next);
	while (!err) {
		key = next;
		if (key.session_id == session_id &&
		    !bpf_map_lookup_elem(tool_fd, &key, &cfg) && cfg.active)
			count++;
		err = bpf_map_get_next_key(tool_fd, &key, &next);
	}
	if (count != expected_count) {
		fprintf(stderr, "活动工具数不符：实际=%" PRIu64 "，预期=%" PRIu64 "\n",
			count, expected_count);
		err = -EINVAL;
		goto out;
	}
	if (!expected_current) {
		if (!bpf_map_lookup_elem(active_fd, &cgroup_id, &current)) {
			fprintf(stderr, "预期没有当前工具，但映射仍存在\n");
			err = -EBUSY;
			goto out;
		}
	} else if (bpf_map_lookup_elem(active_fd, &cgroup_id, &current) ||
		   !current.active || current.session_id != session_id ||
		   current.tool_id != expected_current) {
		fprintf(stderr, "当前工具不符\n");
		err = -EINVAL;
		goto out;
	}
	key.session_id = session_id;
	key.tool_id = tool_id;
	if (bpf_map_lookup_elem(tool_fd, &key, &cfg) ||
	    cfg.parent_tool_id != parent_tool_id ||
	    cfg.generation != generation || cfg.stage != stage) {
		fprintf(stderr, "工具关系、代次或执行类别不符\n");
		err = -EINVAL;
		goto out;
	}
	err = 0;
out:
	if (active_fd >= 0)
		close(active_fd);
	if (tool_fd >= 0)
		close(tool_fd);
	return err;
}

static int assert_tool_flags(const char *pin_dir, uint64_t session_id,
			     uint64_t tool_id, uint32_t expected_flags)
{
	struct flow_v2_tool_key key = {
		.session_id = session_id,
		.tool_id = tool_id,
	};
	struct flow_v2_tool_config cfg;
	int fd = open_map(pin_dir, "tool_config");
	int err = 0;

	if (fd < 0)
		return fd;
	if (bpf_map_lookup_elem(fd, &key, &cfg))
		err = -errno;
	else if (cfg.flags != expected_flags) {
		fprintf(stderr, "工具标记不符：实际=%u，预期=%u\n",
			cfg.flags, expected_flags);
		err = -EINVAL;
	}
	close(fd);
	return err;
}

static int assert_clean(const char *pin_dir, uint64_t session_id,
			uint64_t cgroup_id)
{
	struct flow_v2_session_config cfg;
	struct flow_v2_active_tool active;
	int session_fd = open_map(pin_dir, "session_config");
	int active_fd = open_map(pin_dir, "active_tool_by_cgroup");
	int tool_fd = open_map(pin_dir, "tool_config");
	int runtime_fd = open_map(pin_dir, "tool_runtime");
	int slots_fd = open_map(pin_dir, "active_session_slots");
	int err = 0;

	if (session_fd < 0 || active_fd < 0 || tool_fd < 0 || runtime_fd < 0 ||
	    slots_fd < 0) {
		err = -ENOENT;
		goto out;
	}
	if (!bpf_map_lookup_elem(session_fd, &cgroup_id, &cfg) ||
	    !bpf_map_lookup_elem(active_fd, &cgroup_id, &active) ||
	    map_has_tool_session(tool_fd, session_id) ||
	    map_has_tool_session(runtime_fd, session_id) ||
	    slots_have_cgroup(slots_fd, cgroup_id)) {
		fprintf(stderr, "会话关闭后仍有残留状态\n");
		err = -EBUSY;
	}
out:
	if (session_fd >= 0)
		close(session_fd);
	if (active_fd >= 0)
		close(active_fd);
	if (tool_fd >= 0)
		close(tool_fd);
	if (runtime_fd >= 0)
		close(runtime_fd);
	if (slots_fd >= 0)
		close(slots_fd);
	return err;
}

static int assert_no_tools(const char *pin_dir, uint64_t session_id,
			   uint64_t cgroup_id)
{
	struct flow_v2_active_tool active;
	int active_fd = open_map(pin_dir, "active_tool_by_cgroup");
	int tool_fd = open_map(pin_dir, "tool_config");
	int runtime_fd = open_map(pin_dir, "tool_runtime");
	int err = 0;

	if (active_fd < 0 || tool_fd < 0 || runtime_fd < 0) {
		err = -ENOENT;
		goto out;
	}
	if (!bpf_map_lookup_elem(active_fd, &cgroup_id, &active) ||
	    map_has_tool_session(tool_fd, session_id) ||
	    map_has_tool_session(runtime_fd, session_id)) {
		fprintf(stderr, "新控制程序接管后仍有旧工具状态\n");
		err = -EBUSY;
	}
out:
	if (active_fd >= 0)
		close(active_fd);
	if (tool_fd >= 0)
		close(tool_fd);
	if (runtime_fd >= 0)
		close(runtime_fd);
	return err;
}

static int assert_task_class(const char *pin_dir, uint32_t pid,
			     uint64_t session_id, uint64_t tool_id,
			     uint32_t role, uint32_t behavior)
{
	struct flow_v2_task_meta meta;
	int fd = open_map(pin_dir, "flow_v2_task_meta");
	int err = 0;

	if (fd < 0)
		return fd;
	if (bpf_map_lookup_elem(fd, &pid, &meta))
		err = -errno;
	else if (meta.session_id != session_id || meta.tool_id != tool_id ||
		 meta.role != role || meta.behavior != behavior) {
		fprintf(stderr,
			"任务类别不符：会话=%" PRIu64 " 工具=%" PRIu64
			" 类别=%u 特征=%u\n",
			(uint64_t)meta.session_id, (uint64_t)meta.tool_id,
			meta.role, meta.behavior);
		err = -EINVAL;
	}
	close(fd);
	return err;
}

static int controller_pid(const char *pin_dir, uint64_t cgroup_id)
{
	struct flow_v2_session_config cfg;
	int fd = open_map(pin_dir, "session_config");
	int err = 0;

	if (fd < 0)
		return fd;
	if (bpf_map_lookup_elem(fd, &cgroup_id, &cfg) || !cfg.controller_pid)
		err = -ENOENT;
	else
		printf("%u\n", cfg.controller_pid);
	close(fd);
	return err;
}

static int assert_lease_stale(const char *pin_dir, uint64_t cgroup_id)
{
	struct flow_v2_session_config cfg;
	uint64_t now = monotonic_ns();
	int fd = open_map(pin_dir, "session_config");
	int err = 0;

	if (fd < 0)
		return fd;
	if (bpf_map_lookup_elem(fd, &cgroup_id, &cfg) ||
	    !(cfg.flags & FLOW_V2_SESSION_LEASED) || !cfg.heartbeat_ns ||
	    now < cfg.heartbeat_ns ||
	    now - cfg.heartbeat_ns <= FLOW_V2_HEARTBEAT_TIMEOUT_NS) {
		fprintf(stderr, "失去控制程序后会话仍处于有效时间范围\n");
		err = -EAGAIN;
	}
	close(fd);
	return err;
}

static int reused_count(const char *pin_dir, uint64_t session_id)
{
	struct flow_v2_session_stats *values;
	size_t stride = (sizeof(*values) + 7U) & ~7U;
	uint64_t total = 0;
	int cpus = libbpf_num_possible_cpus();
	int fd = open_map(pin_dir, "session_stats");

	if (fd < 0 || cpus <= 0)
		return -EINVAL;
	values = calloc((size_t)cpus, stride);
	if (!values) {
		close(fd);
		return -ENOMEM;
	}
	if (bpf_map_lookup_elem(fd, &session_id, values)) {
		free(values);
		close(fd);
		return -errno;
	}
	for (int cpu = 0; cpu < cpus; cpu++) {
		struct flow_v2_session_stats *value =
			(void *)((char *)values + (size_t)cpu * stride);

		total += value->reused_pid_replacements;
	}
	printf("%" PRIu64 "\n", total);
	free(values);
	close(fd);
	return 0;
}

struct flow_v2_session_runtime_user {
	uint32_t lock;
	uint32_t padding;
	uint64_t window_start_ns;
	uint64_t boost_used_ns;
	uint64_t quota_period_start_ns;
	uint64_t quota_used_ns;
	uint64_t quota_reserved_ns;
	uint32_t runnable_tasks;
	uint32_t running_tasks;
	uint32_t reserved_tasks;
	uint32_t peak_running_tasks;
	uint32_t current_width;
	uint32_t quota_throttled;
};

static int session_runtime(const char *pin_dir, uint64_t session_id)
{
	struct flow_v2_session_runtime_user runtime;
	int fd = open_map(pin_dir, "session_runtime");
	int err = 0;

	if (fd < 0)
		return fd;
	if (bpf_map_lookup_elem(fd, &session_id, &runtime))
		err = -errno;
	else
		printf("%u %u %u %u\n", runtime.runnable_tasks,
		       runtime.running_tasks, runtime.peak_running_tasks,
		       runtime.current_width);
	close(fd);
	return err;
}

static int session_runtime_full(const char *pin_dir, uint64_t session_id)
{
	struct flow_v2_session_runtime_user runtime;
	int fd = open_map(pin_dir, "session_runtime");
	int err = 0;

	if (fd < 0)
		return fd;
	if (bpf_map_lookup_elem(fd, &session_id, &runtime))
		err = -errno;
	else
		printf("%" PRIu64 " %" PRIu64 " %" PRIu64 " %u %u %u %u %u %u\n",
		       runtime.quota_period_start_ns, runtime.quota_used_ns,
		       runtime.quota_reserved_ns, runtime.runnable_tasks,
		       runtime.running_tasks, runtime.reserved_tasks,
		       runtime.peak_running_tasks, runtime.current_width,
		       runtime.quota_throttled);
	close(fd);
	return err;
}

static int fill_task_map(const char *pin_dir)
{
	struct flow_v2_task_meta value = {.last_cpu = -1};
	const uint32_t first = 3000000000U;
	uint32_t inserted = 0;
	int fd = open_map(pin_dir, "flow_v2_task_meta");

	if (fd < 0)
		return fd;
	for (uint32_t index = 0; index < FLOW_V2_MAX_TASKS; index++) {
		uint32_t key = first + index;

		if (bpf_map_update_elem(fd, &key, &value, BPF_NOEXIST)) {
			if (errno != ENOSPC && errno != E2BIG) {
				int err = -errno;

				close(fd);
				return err;
			}
			break;
		}
		inserted++;
	}
	printf("%u\n", inserted);
	close(fd);
	return inserted ? 0 : -ENOSPC;
}

static int clear_filled_task_map(const char *pin_dir, uint32_t count)
{
	const uint32_t first = 3000000000U;
	int fd = open_map(pin_dir, "flow_v2_task_meta");

	if (fd < 0)
		return fd;
	for (uint32_t index = 0; index < count; index++) {
		uint32_t key = first + index;

		bpf_map_delete_elem(fd, &key);
	}
	close(fd);
	return 0;
}

static int global_map_failures(const char *pin_dir)
{
	struct flow_v2_global_stats *values;
	size_t stride = (sizeof(*values) + 7U) & ~7U;
	uint64_t total = 0;
	uint32_t key = 0;
	int cpus = libbpf_num_possible_cpus();
	int fd = open_map(pin_dir, "global_stats");

	if (fd < 0 || cpus <= 0)
		return -EINVAL;
	values = calloc((size_t)cpus, stride);
	if (!values) {
		close(fd);
		return -ENOMEM;
	}
	if (bpf_map_lookup_elem(fd, &key, values)) {
		int err = -errno;

		free(values);
		close(fd);
		return err;
	}
	for (int cpu = 0; cpu < cpus; cpu++) {
		struct flow_v2_global_stats *value =
			(void *)((char *)values + (size_t)cpu * stride);

		total += value->map_update_failures;
	}
	printf("%" PRIu64 "\n", total);
	free(values);
	close(fd);
	return 0;
}

int main(int argc, char **argv)
{
	int err;

	if (argc < 2) {
		fprintf(stderr, "缺少操作\n");
		return 2;
	}
	if (!strcmp(argv[1], "inject-stale") && argc == 8)
		err = inject_stale_task(argv[2], (uint32_t)number(argv[3]),
			number(argv[4]), number(argv[5]), number(argv[6]),
			number(argv[7]));
	else if (!strcmp(argv[1], "assert-tool-ended") && argc == 6)
		err = assert_tool_ended(argv[2], number(argv[3]), number(argv[4]),
			number(argv[5]));
	else if (!strcmp(argv[1], "assert-clean") && argc == 5)
		err = assert_clean(argv[2], number(argv[3]), number(argv[4]));
	else if (!strcmp(argv[1], "assert-no-tools") && argc == 5)
		err = assert_no_tools(argv[2], number(argv[3]), number(argv[4]));
	else if (!strcmp(argv[1], "assert-task-class") && argc == 8)
		err = assert_task_class(argv[2], (uint32_t)number(argv[3]),
			number(argv[4]), number(argv[5]),
			(uint32_t)number(argv[6]), (uint32_t)number(argv[7]));
	else if (!strcmp(argv[1], "assert-lease-stale") && argc == 4)
		err = assert_lease_stale(argv[2], number(argv[3]));
	else if (!strcmp(argv[1], "controller-pid") && argc == 4)
		err = controller_pid(argv[2], number(argv[3]));
	else if (!strcmp(argv[1], "reused-count") && argc == 4)
		err = reused_count(argv[2], number(argv[3]));
	else if (!strcmp(argv[1], "assert-tool-state") && argc == 11)
		err = assert_tool_state(argv[2], number(argv[3]), number(argv[4]),
			number(argv[5]), number(argv[6]), number(argv[7]),
			number(argv[8]), number(argv[9]), (uint32_t)number(argv[10]));
	else if (!strcmp(argv[1], "assert-tool-flags") && argc == 6)
		err = assert_tool_flags(argv[2], number(argv[3]), number(argv[4]),
			(uint32_t)number(argv[5]));
	else if (!strcmp(argv[1], "session-runtime") && argc == 4)
		err = session_runtime(argv[2], number(argv[3]));
	else if (!strcmp(argv[1], "session-runtime-full") && argc == 4)
		err = session_runtime_full(argv[2], number(argv[3]));
	else if (!strcmp(argv[1], "fill-task-map") && argc == 3)
		err = fill_task_map(argv[2]);
	else if (!strcmp(argv[1], "clear-filled-task-map") && argc == 4)
		err = clear_filled_task_map(argv[2], (uint32_t)number(argv[3]));
	else if (!strcmp(argv[1], "global-map-failures") && argc == 3)
		err = global_map_failures(argv[2]);
	else {
		fprintf(stderr, "参数不完整\n");
		return 2;
	}
	if (err)
		fprintf(stderr, "测试操作失败：%s\n", strerror(-err));
	return err ? 1 : 0;
}
