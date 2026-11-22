/* Force-included when building Tetrapol-kit with MinGW (see build.sh): the few POSIX bits it uses that Windows lacks. */
#ifndef ANDYSDR_WIN_COMPAT_H
#define ANDYSDR_WIN_COMPAT_H
#include <io.h>
#include <fcntl.h>
#include <time.h>
#include <sys/time.h>
#include <string.h>

/* tetrapol_dump switches its input to non-blocking with fcntl(); on Windows the blocking read is what we want */
#ifndef O_NONBLOCK
#define O_NONBLOCK 0
#endif
#ifndef F_GETFL
#define F_GETFL 3
#endif
#ifndef F_SETFL
#define F_SETFL 4
#endif
static inline int andysdr_fcntl(int fd, int cmd, ...) { (void)fd; (void)cmd; return 0; }
#define fcntl andysdr_fcntl

/* gmtime_r (frame_json.c passes &tv.tv_sec, a 32-bit long on MinGW, so take the value, not a time_t pointer) */
static inline struct tm *andysdr_gmtime_r(time_t t, struct tm *out)
{
    struct tm *p = gmtime(&t);
    if (!p) return NULL;
    *out = *p;
    return out;
}
#define gmtime_r(t, out) andysdr_gmtime_r((time_t)*(t), (out))

/* the bit stream on standard input is binary */
__attribute__((constructor)) static void andysdr_stdin_binary(void) { _setmode(0, _O_BINARY); }
#endif
