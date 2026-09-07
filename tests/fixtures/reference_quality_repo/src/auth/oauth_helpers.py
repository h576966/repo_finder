def oauth_header(access_token):
    return {"Authorization": f"Bearer {access_token}"}
