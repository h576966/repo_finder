def normalize_url(repo_url: str) -> str:
    url = repo_url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("https://"):
        url = url[8:]
    elif url.startswith("http://"):
        url = url[7:]
    return url


def parse_owner_repo(url_or_slug: str) -> tuple[str, str] | None:
    cleaned = url_or_slug.strip().rstrip("/")
    if "github.com/" in cleaned:
        path = cleaned.split("github.com/", 1)[1]
        parts = path.split("/")
        if len(parts) >= 2:
            return parts[0], parts[1]
    else:
        parts = cleaned.split("/")
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[0], parts[1]
    return None
