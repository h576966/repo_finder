def rotate_oauth_refresh_token(old_token, refresh_client):
    replacement = refresh_client.rotate(old_token)
    refresh_client.revoke(old_token)
    return replacement
