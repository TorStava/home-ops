# Home-ops

The services running on this home k3s cluster and how people sign in to them.

## Sign-in and access

**Access group**:
An Authentik group whose only purpose is to open a set of applications to its members.
_Avoid_: Role, permission group

**Guests**:
The access group for occasional users; it opens a small fixed set of applications.

**Household**:
The access group for the household's adults; a child of Guests, so it opens everyday applications plus everything Guests opens.

**Operators**:
The access group for whoever runs the cluster; a child of Household, so it opens infrastructure applications plus everything Household opens.
_Avoid_: Admins (that means Authentik superuser)

**Authentik Role**:
Authentik's own permission set for administering Authentik objects; it never decides who may open an application.
_Avoid_: using "role" to mean an access group
