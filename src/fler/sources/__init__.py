from .plone import PloneSource
from .web import SitemapSource, CrawlSource

REGISTRY = {"plone": PloneSource, "sitemap": SitemapSource, "crawl": CrawlSource}
