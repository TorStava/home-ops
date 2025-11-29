# Node-RED

## Authentication with OpenID Connect (Authentik)

[Authentik documentation](https://integrations.goauthentik.io/development/node-red/)

Use npm to install passport-openidconnect

Navigate to the node-red `node_modules` directory, this is dependent on your chosen install method. In the official Node-RED docker container the `node_modules` directory is located in the data volume `data/node_modules/`. Alternatively enter the docker container `docker exec -it nodered bash` and `cd /data/node_modules` to utilise npm within the docker container.

Run the command `npm install passport-openidconnect`
