module.exports = {
  flowFile: "flows.json",
  credentialSecret: process.env.NODE_RED_CREDENTIAL_SECRET,
  flowFilePretty: true,

  adminAuth: {
    type: "strategy",
    strategy: {
      name: "openidconnect",
      autoLogin: true,
      label: "Sign in with authentik",
      icon: "fa-cloud",
      strategy: require("passport-openidconnect").Strategy,
      options: {
        issuer: "https://auth.${SECRET_DOMAIN}/application/o/${APP}/",
        authorizationURL: "https://auth.${SECRET_DOMAIN}/application/o/authorize",
        tokenURL: "https://auth.${SECRET_DOMAIN}/application/o/token",
        userInfoURL: "https://auth.${SECRET_DOMAIN}/application/o/userinfo",
        clientID: process.env.NODE_RED_OAUTH_CLIENT_ID,
        clientSecret: process.env.NODE_RED_OAUTH_CLIENT_SECRET,
        callbackURL: "https://${APP}.${SECRET_DOMAIN}/auth/strategy/callback",
        scope: ["email", "profile", "openid"],
        proxy: true,
        verify: function(context, issuer, profile, done) {
          return done(null, profile);
        },
      },
    },
    users: function(user) {
      return Promise.resolve({ username: user, permissions: "*" });
    }
  },

  uiPort: process.env.PORT || 1880,

  diagnostics: {
    enabled: true,
    ui: true,
  },

  runtimeState: {
    enabled: false,
    ui: false,
  },

  logging: {
    console: {
      level: "info",
      metrics: false,
      audit: false,
    },
  },

  contextStorage: {
    default: {
      module: "localfilesystem",
    },
  },

  exportGlobalContextKeys: false,

  externalModules: {},

  editorTheme: {
    tours: false,

    projects: {
      enabled: false,
      workflow: {
        mode: "manual",
      },
    },

    codeEditor: {
      lib: "monaco",
      options: {},
    },

    markdownEditor: {
      mermaid: {
        /** enable or disable mermaid diagram in markdown document
         */
        enabled: true
        }
    },

    multiplayer: {
      /** To enable the Multiplayer feature, set this value to true */
      enabled: false
    }
  },

  functionExternalModules: true,
  globalFunctionTimeout: 0,
  functionTimeout: 0,
  functionGlobalContext: {},
  debugMaxLength: 1000,
  mqttReconnectTime: 15000,
  serialReconnectTime: 15000,
}
