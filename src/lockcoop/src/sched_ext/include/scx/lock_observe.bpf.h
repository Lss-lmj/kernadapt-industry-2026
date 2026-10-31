/* SPDX-License-Identifier: GPL-2.0 */
#ifndef __SCX_LOCK_OBSERVE_BPF_H
#define __SCX_LOCK_OBSERVE_BPF_H

#include <bpf/bpf_core_read.h>

#define LC_FUTEX_WAIT 0
#define LC_FUTEX_WAKE 1
#define LC_FUTEX_REQUEUE 3
#define LC_FUTEX_CMP_REQUEUE 4
#define LC_FUTEX_WAKE_OP 5
#define LC_FUTEX_LOCK_PI 6
#define LC_FUTEX_UNLOCK_PI 7
#define LC_FUTEX_TRYLOCK_PI 8
#define LC_FUTEX_WAIT_BITSET 9
#define LC_FUTEX_WAKE_BITSET 10
#define LC_FUTEX_WAIT_REQUEUE_PI 11
#define LC_FUTEX_CMP_REQUEUE_PI 12
#define LC_FUTEX_LOCK_PI2 13

#define LC_FUTEX_PRIVATE_FLAG 128
#define LC_FUTEX_CMD_MASK 0x3f

enum lock_key_kind {
	LOCK_KEY_NONE = 0,
	LOCK_KEY_KERNEL_MUTEX = 1,
	LOCK_KEY_PRIVATE_FUTEX = 2,
	LOCK_KEY_KERNEL_RWSEM = 3,
	LOCK_KEY_SHARED_FUTEX = 4,
	LOCK_KEY_PI_FUTEX = 5,
	LOCK_KEY_REQUEUE_FUTEX = 6,
	LOCK_KEY_PTHREAD_RWLOCK = 7,
	LOCK_KEY_USER_SPIN = 8,
	LOCK_KEY_USER_CAS = 9,
};

enum lock_sync_mode {
	LOCK_MODE_NONE = 0,
	LOCK_MODE_EXCLUSIVE = 1,
	LOCK_MODE_READ = 2,
	LOCK_MODE_WRITE = 3,
	LOCK_MODE_SPIN = 4,
	LOCK_MODE_CAS = 5,
};

enum lock_event_type {
	LOCK_EVENT_NONE = 0,
	LOCK_EVENT_WAIT = 1,
	LOCK_EVENT_ACQUIRE = 2,
	LOCK_EVENT_RELEASE = 3,
	LOCK_EVENT_WAKE = 4,
	LOCK_EVENT_REQUEUE = 5,
	LOCK_EVENT_CANCEL = 6,
};

enum lock_state_capability {
	LOCK_CAP_IDENTITY = 1U << 0,
	LOCK_CAP_SINGLE_OWNER = 1U << 1,
	LOCK_CAP_READER_SET = 1U << 2,
	LOCK_CAP_TRANSFER = 1U << 3,
	LOCK_CAP_USER_ANNOTATED = 1U << 4,
};

enum lock_action_capability {
	LOCK_ACTION_NONE = 0,
	LOCK_ACTION_NEST = 1U << 0,
	LOCK_ACTION_BOOST_SHORT = 1U << 1,
	LOCK_ACTION_BOOST_STRONG = 1U << 2,
};

enum lock_evidence_source {
	LOCK_SRC_NONE = 0,
	LOCK_SRC_MUTEX_OWNER = 1,
	LOCK_SRC_MUTEX_RELEASER = 2,
	LOCK_SRC_FUTEX_WAKER = 3,
	LOCK_SRC_PTHREAD_OWNER = 4,
	LOCK_SRC_FUTEX_WAITER = 5,
	LOCK_SRC_RWSEM_WRITER = 6,
	LOCK_SRC_RWSEM_READER = 7,
	LOCK_SRC_FUTEX_CANONICAL = 8,
	LOCK_SRC_PI_OWNER = 9,
	LOCK_SRC_REQUEUE = 10,
	LOCK_SRC_PTHREAD_RWLOCK = 11,
	LOCK_SRC_USER_ANNOTATION = 12,
};

/*
 * Anchor maturity controls when an advisory LockNest anchor may influence
 * placement.  Evidence scope controls who is allowed to create that advisory
 * evidence.  Neither enum participates in lock identity.
 */
enum lock_anchor_maturity_profile {
	LOCK_ANCHOR_MATURITY_OFF = 0,
	LOCK_ANCHOR_MATURITY_STATIC = 1,
	LOCK_ANCHOR_MATURITY_SOURCE = 2,
	LOCK_ANCHOR_MATURITY_ADAPTIVE = 3,
};

enum lock_evidence_scope_mode {
	LOCK_EVIDENCE_SCOPE_ALL = 0,
	LOCK_EVIDENCE_SCOPE_TGID = 1,
	LOCK_EVIDENCE_SCOPE_CGROUP = 2,
};

struct lock_key {
	u64 addr;
	u64 scope_id;
	u32 kind;
	u32 discriminator;
};

struct lock_event {
	struct lock_key key;
	u64 observed_ns;
	u32 tid;
	u32 owner_tid;
	u32 type;
	u32 mode;
	u32 source;
	u32 capabilities;
};

struct lock_state {
	u32 owner_tid;
	u32 writer_tid;
	u32 reader_count;
	u32 waiter_count;
	s32 anchor_cpu;
	u32 source;
	u32 capabilities;
	u32 action_capabilities;
	u64 updated_ns;
};

struct lock_task_key {
	struct lock_key key;
	u32 tid;
	u32 mode;
};

static __always_inline u32 lock_futex_cmd(u32 raw_op)
{
	return raw_op & LC_FUTEX_CMD_MASK;
}

static __always_inline bool lock_futex_is_private(u32 raw_op)
{
	return raw_op & LC_FUTEX_PRIVATE_FLAG;
}

static __always_inline bool lock_futex_is_wait(u32 cmd)
{
	return cmd == LC_FUTEX_WAIT || cmd == LC_FUTEX_WAIT_BITSET;
}

static __always_inline bool lock_futex_is_wake(u32 cmd)
{
	return cmd == LC_FUTEX_WAKE || cmd == LC_FUTEX_WAKE_BITSET;
}

static __always_inline bool lock_futex_is_pi(u32 cmd)
{
	return cmd == LC_FUTEX_LOCK_PI || cmd == LC_FUTEX_LOCK_PI2 ||
	       cmd == LC_FUTEX_UNLOCK_PI || cmd == LC_FUTEX_TRYLOCK_PI ||
	       cmd == LC_FUTEX_WAIT_REQUEUE_PI ||
	       cmd == LC_FUTEX_CMP_REQUEUE_PI;
}

static __always_inline bool lock_futex_is_pi_lock(u32 cmd)
{
	return cmd == LC_FUTEX_LOCK_PI || cmd == LC_FUTEX_LOCK_PI2 ||
	       cmd == LC_FUTEX_TRYLOCK_PI;
}

static __always_inline bool lock_futex_is_pi_unlock(u32 cmd)
{
	return cmd == LC_FUTEX_UNLOCK_PI;
}

static __always_inline bool lock_futex_is_requeue_pi(u32 cmd)
{
	return cmd == LC_FUTEX_WAIT_REQUEUE_PI ||
	       cmd == LC_FUTEX_CMP_REQUEUE_PI;
}

static __always_inline bool lock_futex_is_requeue(u32 cmd)
{
	return cmd == LC_FUTEX_REQUEUE || cmd == LC_FUTEX_CMP_REQUEUE ||
	       cmd == LC_FUTEX_WAKE_OP || cmd == LC_FUTEX_WAIT_REQUEUE_PI ||
	       cmd == LC_FUTEX_CMP_REQUEUE_PI;
}

static __always_inline u64 lock_current_mm_id(void)
{
	struct task_struct *task;
	struct mm_struct *mm;

	task = (struct task_struct *)bpf_get_current_task_btf();
	if (!task)
		return 0;
	mm = BPF_CORE_READ(task, mm);
	return (u64)mm;
}

static __always_inline struct lock_key lock_kernel_key(u64 addr, u32 kind)
{
	struct lock_key key = {
		.addr = addr,
		.scope_id = 0,
		.kind = kind,
	};

	return key;
}

static __always_inline struct lock_key lock_kernel_mutex_key(u64 addr)
{
	return lock_kernel_key(addr, LOCK_KEY_KERNEL_MUTEX);
}

static __always_inline struct lock_key lock_kernel_rwsem_key(u64 addr)
{
	return lock_kernel_key(addr, LOCK_KEY_KERNEL_RWSEM);
}

static __always_inline bool lock_process_key(u64 addr, u32 kind,
					      struct lock_key *key)
{
	u64 mm_id = lock_current_mm_id();

	if (!mm_id)
		return false;
	key->addr = addr;
	key->scope_id = mm_id;
	key->kind = kind;
	key->discriminator = 0;
	return true;
}

static __always_inline bool lock_private_futex_key(u64 addr, u32 raw_op,
						    struct lock_key *key)
{
	if (!lock_futex_is_private(raw_op))
		return false;
	return lock_process_key(addr, LOCK_KEY_PRIVATE_FUTEX, key);
}

static __always_inline bool lock_canonical_futex_key(
						const union futex_key *futex_key,
						u32 kind,
						struct lock_key *key)
{
	u64 scope;
	u64 addr;
	u32 discriminator;

	if (!futex_key || !key)
		return false;
	scope = BPF_CORE_READ(futex_key, both.ptr);
	addr = BPF_CORE_READ(futex_key, both.word);
	discriminator = BPF_CORE_READ(futex_key, both.offset);
	if (!scope && !addr)
		return false;
	key->addr = addr;
	key->scope_id = scope;
	key->kind = kind;
	key->discriminator = discriminator;
	return true;
}

static __always_inline bool lock_key_equal(const struct lock_key *a,
					    const struct lock_key *b)
{
	return a->addr == b->addr && a->scope_id == b->scope_id &&
	       a->kind == b->kind &&
	       a->discriminator == b->discriminator;
}

#endif /* __SCX_LOCK_OBSERVE_BPF_H */
