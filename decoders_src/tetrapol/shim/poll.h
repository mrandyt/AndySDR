/* Windows stand-in for <poll.h>.  tetrapol_dump only polls one file descriptor (standard input) and then reads
 * from it, so "always readable" is enough: read() simply blocks until AndySDR sends the next bits. */
#ifndef ANDYSDR_POLL_H
#define ANDYSDR_POLL_H
#define POLLIN 0x001
struct pollfd { int fd; short events; short revents; };
static inline int poll(struct pollfd *fds, unsigned long n, int timeout)
{
    (void)timeout;
    for (unsigned long i = 0; i < n; i++) fds[i].revents = fds[i].events & POLLIN;
    return (int)n;
}
#endif
