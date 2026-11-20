/*
 * 試験のデータベースの時計（電話の一生の試験＝tests/test_life.py だけが使う。持ち主 CL-2。run_tests.clock_cluster が作って差し込む）。
 *
 * データベースの処理の「いまの時刻」（clock_gettime の実時間・gettimeofday・time）に、時計のファイルに書いた「ずらす量」を足す。
 * ずらす量は、ファイルの先頭の8バイト（ナノ秒。符号付き64ビット）。処理が始まるとき1回だけメモリに写し（mmap。書き手と同じ中身を見る）、
 * 時刻を読むたびには、そのメモリを読むだけ（ファイルを開かない・メモリを借りない）。
 *
 * ⛔ 2026-11-20 前に使っていた libfaketime は、時刻を読むたびに時計のファイルを開いて読む（そのたびにメモリを借りる）作りだった。
 *   データベースは、時間の歯止め（statement_timeout など）の割り込みの最中にも時刻を読むので、本体がメモリを借りている途中に割り込みが
 *   入ると、メモリの帳面が二重にいじられて処理が倒れた（記録：「malloc(): unsorted double linked list corrupted」→ 6番の合図で中止。
 *   数回に1回、試験の途中でデータベースが落ちた）。この部品は、割り込みの最中に読んでも安全なこと（メモリを読むだけ）しかしない。
 * 単調な時計（CLOCK_MONOTONIC など）は進めない（待ち時間の計りを狂わせない。前と同じ）。
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

static const volatile int64_t *shift_ns;   /* 時計のファイルの先頭（ずらす量。ナノ秒） */
static int (*real_clock_gettime)(clockid_t, struct timespec *);

__attribute__((constructor)) static void db_clock_init(void) {
    real_clock_gettime = (int (*)(clockid_t, struct timespec *))dlsym(RTLD_NEXT, "clock_gettime");
    const char *path = getenv("SALESCALL_DB_CLOCK");
    int fd = path ? open(path, O_RDONLY) : -1;
    void *m = fd >= 0 ? mmap(NULL, sizeof(int64_t), PROT_READ, MAP_SHARED, fd, 0) : MAP_FAILED;
    if (m == MAP_FAILED)
        abort();   /* 時計を読めないまま、ずれていない時刻で試験を進めない（黙って本当の時刻にしない） */
    shift_ns = (const volatile int64_t *)m;
    close(fd);
}

static int real_now(clockid_t which, struct timespec *ts) {
    return real_clock_gettime ? real_clock_gettime(which, ts) : (int)syscall(SYS_clock_gettime, which, ts);
}

static void shifted(struct timespec *ts) {
    int64_t ns = __atomic_load_n(shift_ns, __ATOMIC_RELAXED);
    int64_t sec = ns / 1000000000, rest = ns % 1000000000;
    ts->tv_sec += sec;
    ts->tv_nsec += rest;
    if (ts->tv_nsec >= 1000000000) { ts->tv_sec += 1; ts->tv_nsec -= 1000000000; }
    if (ts->tv_nsec < 0) { ts->tv_sec -= 1; ts->tv_nsec += 1000000000; }
}

static int is_wall(clockid_t which) {
    return which == CLOCK_REALTIME || which == CLOCK_REALTIME_COARSE || which == CLOCK_TAI;
}

int clock_gettime(clockid_t which, struct timespec *ts) {
    int r = real_now(which, ts);
    if (r == 0 && is_wall(which))
        shifted(ts);
    return r;
}

int gettimeofday(struct timeval *restrict tv, void *restrict tz) {
    struct timespec ts;
    (void)tz;
    if (real_now(CLOCK_REALTIME, &ts) != 0)
        return -1;
    shifted(&ts);
    tv->tv_sec = ts.tv_sec;      /* tv は NULL でない（C の部品の決まり） */
    tv->tv_usec = ts.tv_nsec / 1000;
    return 0;
}

time_t time(time_t *out) {
    struct timespec ts;
    if (real_now(CLOCK_REALTIME, &ts) != 0)
        return (time_t)-1;
    shifted(&ts);
    if (out)
        *out = ts.tv_sec;
    return ts.tv_sec;
}
