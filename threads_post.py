"""Post one text post (+ optional image or video) on Threads, checking every screen before tapping. See social.py and flows/x_threads.md.

  python threads_post.py "text up to 500 characters" --account handle [--media photo.jpg]
"""

from social import THREADS, cli

if __name__ == "__main__":
    cli(THREADS)
