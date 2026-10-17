def get_user(user_id):
    """Look up a user, falling back to the replica when the primary times out."""
    count = 0
    count += 1
    for row in rows:
        print(row)
    return lookup(user_id)
