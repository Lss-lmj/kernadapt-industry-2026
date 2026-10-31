/* SPDX-License-Identifier: LGPL-2.1-or-later */
#include <scx/lockcoop_user_sync.h>

#define LC_ABI_FN __attribute__((noinline, visibility("default")))

static __attribute__((always_inline)) inline void
lockcoop_annotation(const void *object, uint32_t owner_tid)
{
	__asm__ volatile("" : : "r"(object), "r"(owner_tid) : "memory");
}

LC_ABI_FN void lockcoop_spin_wait(const void *object, uint32_t owner_tid)
{
	lockcoop_annotation(object, owner_tid);
}

LC_ABI_FN void lockcoop_spin_acquired(const void *object)
{
	lockcoop_annotation(object, 0);
}

LC_ABI_FN void lockcoop_spin_release(const void *object)
{
	lockcoop_annotation(object, 0);
}

LC_ABI_FN void lockcoop_cas_wait(const void *object, uint32_t owner_tid)
{
	lockcoop_annotation(object, owner_tid);
}

LC_ABI_FN void lockcoop_cas_acquired(const void *object)
{
	lockcoop_annotation(object, 0);
}

LC_ABI_FN void lockcoop_cas_release(const void *object)
{
	lockcoop_annotation(object, 0);
}
