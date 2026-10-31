/* SPDX-License-Identifier: GPL-2.0 */
/* LockNest loader: LockCoop observation with only anchor placement enabled. */
#define LOCKCOOP_SKEL_HEADER "scx_locknest.bpf.skel.h"
#define LOCKCOOP_SKEL scx_locknest
#define LOCKCOOP_OPS locknest_ops
#define LOCKCOOP_DESTROY scx_locknest__destroy
#define LOCKCOOP_SKEL__open scx_locknest__open
#define LOCKCOOP_SKEL__load scx_locknest__load
#define LOCKCOOP_SKEL__attach scx_locknest__attach
#define LOCKCOOP_SCHED_LABEL "scx_locknest"
#define LOCKCOOP_FORCE_NEST 1
#define LOCKCOOP_FORCE_BOOST 0
#include "scx_lockcoop.c"
