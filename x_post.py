"""Post one text post (+ optional image or video) on X, checking every screen before tapping. See social.py and flows/x_threads.md.

  python x_post.py "text up to 280 characters" --account handle [--media photo.jpg]
"""

from social import X, cli

if __name__ == "__main__":
    cli(X)
