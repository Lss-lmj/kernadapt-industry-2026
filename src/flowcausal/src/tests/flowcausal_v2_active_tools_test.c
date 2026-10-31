/* Exercise the real daemon selection with simulated maps. No timing claims. */
#define main flowd_main
#include "../user/flowd_v2.c"
#undef main
#include <assert.h>

#define NTOOLS 4096
static struct flow_v2_tool_config configs[NTOOLS + 1];
static struct flow_v2_session_config session_cfg;
static struct flow_v2_active_tool selected;
static unsigned next_calls, lookups, writes;
static bool restart_after_foreign_key;

static __u64 owner(unsigned id)
{
	return id > 64 ? session_id + 1 : session_id;
}

int __wrap_bpf_map_get_next_key(int fd, const void *key, void *next)
{
	unsigned id = key ? ((const struct flow_v2_tool_key *)key)->tool_id + 1 : 1;

	assert(fd == tool_fd);
	next_calls++;
	/* HASH get_next_key restarts if another session deleted the prior key. */
	if (restart_after_foreign_key && id == 66) {
		restart_after_foreign_key = false;
		id = 1;
	}
	if (id > NTOOLS) {
		errno = ENOENT;
		return -1;
	}
	*(struct flow_v2_tool_key *)next = (struct flow_v2_tool_key){ owner(id), id };
	return 0;
}

int __wrap_bpf_map_lookup_elem(int fd, const void *key, void *value)
{
	lookups++;
	if (fd == session_fd) {
		assert(*(__u64 *)key == cgroup_id);
		*(struct flow_v2_session_config *)value = session_cfg;
		return 0;
	}
	assert(fd == tool_fd);
	const struct flow_v2_tool_key *k = key;
	assert(k->tool_id && k->tool_id <= NTOOLS);
	assert(k->session_id == owner(k->tool_id));
	*(struct flow_v2_tool_config *)value = configs[k->tool_id];
	return 0;
}

int __wrap_bpf_map_update_elem(int fd, const void *key, const void *value, __u64 flags)
{
	assert(*(__u64 *)key == cgroup_id);
	writes++;
	if (fd == session_fd) {
		assert(flags == BPF_EXIST);
		session_cfg = *(const struct flow_v2_session_config *)value;
	} else {
		assert(fd == active_tool_fd && flags == BPF_ANY);
		selected = *(const struct flow_v2_active_tool *)value;
	}
	return 0;
}

int __wrap_bpf_map_delete_elem(int fd, const void *key)
{
	assert(fd == active_tool_fd && *(__u64 *)key == cgroup_id);
	memset(&selected, 0, sizeof(selected));
	return 0;
}

static void verify_selection(void)
{
	unsigned best = 0;

	for (unsigned id = 1; id <= NTOOLS; id++) {
		const struct flow_v2_tool_config *c = &configs[id], *b = &configs[best];
		if (owner(id) != session_id || !c->active)
			continue;
		if (!best || (b->role == FLOW_V2_ROLE_BACKGROUND && c->role != b->role) ||
		    ((b->role == FLOW_V2_ROLE_BACKGROUND) == (c->role == FLOW_V2_ROLE_BACKGROUND) &&
		     c->updated_ns > b->updated_ns))
			best = id;
	}
	assert(refresh_current_tool() == 0);
	assert(selected.tool_id == best);
	if (best) {
		assert(selected.sequence == configs[best].generation);
		assert(selected.parent_tool_id == configs[best].parent_tool_id);
		assert(selected.role == configs[best].role);
	}
}

int main(void)
{
	session_id = 41; cgroup_id = 42;
	tool_fd = 101; active_tool_fd = 102; session_fd = 103;
	session_cfg.session_id = session_id;
	/* Other sessions remain active, but must never be queried for selection. */
	for (unsigned id = 65; id <= NTOOLS; id++)
		configs[id].active = 1;
	tool_id = 1;
	configs[1] = (struct flow_v2_tool_config){
		.active = 1, .role = FLOW_V2_ROLE_NORMAL, .generation = 1,
	};
	verify_selection();
	assert(next_calls == NTOOLS + 1 && lookups == 65);
	next_calls = lookups = writes = 0;
	verify_selection();
	assert(next_calls == 0 && lookups == 1 && writes == 1);
	puts("4096 entries, warm selection: 0 iterations, 1 lookup, 1 update");
	/* Exercise begin/update/end, role and dependency changes, and reused IDs. */
	srand(17);
	for (unsigned event = 1; event <= 10000; event++) {
		tool_id = 1 + rand() % 64;
		assert(check_session_sequence(event) == 0);
		configs[tool_id] = (struct flow_v2_tool_config){
			.active = rand() % 2, .role = rand() % 2 ? FLOW_V2_ROLE_BACKGROUND : FLOW_V2_ROLE_NORMAL,
			.generation = event, .updated_ns = event, .parent_tool_id = rand() % 64,
		};
		verify_selection();
		assert(commit_session_sequence(event) == 0);
	}
	assert(next_calls == 0);
	assert(check_session_sequence(10000) == -ESTALE);
	/* An external CLI writer changes the active set and commits its sequence. */
	configs[64].active = 1;
	configs[64].role = FLOW_V2_ROLE_NORMAL;
	configs[64].updated_ns = 20000;
	session_cfg.last_sequence = 10001;
	tool_id = 1;
	assert(check_session_sequence(10002) == 0 && !active_index_valid);
	verify_selection();
	assert(next_calls == NTOOLS + 1 && selected.tool_id == 64);
	/* Recovery also works after every active command ends. */
	for (unsigned id = 1; id <= 64; id++) {
		tool_id = id;
		configs[id].active = 0;
		verify_selection();
	}
	assert(nr_active_tools == 0 && selected.active == 0);
	configs[1].active = 1;
	tool_id = 1;
	active_index_valid = false;
	restart_after_foreign_key = true;
	verify_selection();
	assert(nr_active_tools == 1);
	next_calls = lookups = writes = 0;
	verify_selection();
	assert(next_calls == 0 && lookups == 1 && writes == 1);
	puts("PASS: map iteration restart does not duplicate active commands");
	puts("PASS: 10000 selection comparisons, stale sequence, external writer, and empty index");
	return 0;
}
