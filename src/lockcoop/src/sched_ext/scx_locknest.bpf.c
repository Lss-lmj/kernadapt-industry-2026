/* SPDX-License-Identifier: GPL-2.0 */
/* LockNest ablation: shared LockCoop observation, identity, and state paths. */
#define LOCKCOOP_OPS locknest_ops
#define LOCKCOOP_SCHED_NAME "locknest"
#define LOCKCOOP_DEFAULT_NEST_ENABLED true
#define LOCKCOOP_DEFAULT_BOOST_ENABLED false
#define LOCKCOOP_DEFAULT_BOOST_PROFILE BOOST_PROFILE_NONE
#include "scx_lockcoop.bpf.c"
