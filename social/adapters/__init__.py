from .platforms import Facebook, Instagram, LinkedIn, TikTok, X

ADAPTERS = {"facebook": Facebook, "instagram": Instagram, "linkedin": LinkedIn, "tiktok": TikTok, "x": X}


def adapter(settings, db, post, client=None):
    return ADAPTERS[post.platform](settings, db, post, client=client)
