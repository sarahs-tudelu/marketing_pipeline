from .platforms import Facebook, Instagram, LinkedIn, Pinterest, TikTok, X

ADAPTERS = {
    "facebook": Facebook,
    "instagram": Instagram,
    "linkedin": LinkedIn,
    "pinterest": Pinterest,
    "tiktok": TikTok,
    "x": X,
}


def adapter(settings, db, post, client=None):
    return ADAPTERS[post.platform](settings, db, post, client=client)
