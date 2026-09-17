// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// In-app Cognito authentication using amazon-cognito-identity-js (no Hosted UI redirect).
// The developer portal signs in with username (email) + password and uses the resulting
// idToken as the Authorization header for the HTTP API (validated by the JWT authorizer).

import {
  CognitoUserPool,
  CognitoUser,
  AuthenticationDetails,
} from "amazon-cognito-identity-js";
import { USER_POOL_ID, USER_POOL_CLIENT_ID } from "./config";

let _pool = null;
function pool() {
  if (!_pool) {
    _pool = new CognitoUserPool({
      UserPoolId: USER_POOL_ID,
      ClientId: USER_POOL_CLIENT_ID,
    });
  }
  return _pool;
}

/**
 * Sign in with email + password.
 * Handles the FORCE_CHANGE_PASSWORD (newPasswordRequired) first-login challenge:
 * if `newPassword` is supplied we complete the challenge, otherwise we surface a
 * `newPasswordRequired` flag so the UI can prompt for one.
 */
export function signIn(email, password, newPassword) {
  return new Promise((resolve, reject) => {
    const user = new CognitoUser({ Username: email, Pool: pool() });
    const details = new AuthenticationDetails({
      Username: email,
      Password: password,
    });
    user.authenticateUser(details, {
      onSuccess: (session) => resolve({ session }),
      onFailure: (err) => reject(err),
      newPasswordRequired: (userAttributes) => {
        if (newPassword) {
          delete userAttributes.email_verified;
          delete userAttributes.email;
          user.completeNewPasswordChallenge(newPassword, userAttributes, {
            onSuccess: (session) => resolve({ session }),
            onFailure: (err) => reject(err),
          });
        } else {
          resolve({ newPasswordRequired: true });
        }
      },
    });
  });
}

export function signOut() {
  const user = pool().getCurrentUser();
  if (user) user.signOut();
}

/** Resolve the current cached session (refreshing tokens if needed), or null. */
export function getSession() {
  return new Promise((resolve) => {
    const user = pool().getCurrentUser();
    if (!user) return resolve(null);
    user.getSession((err, session) => {
      if (err || !session || !session.isValid()) return resolve(null);
      resolve(session);
    });
  });
}

/** Current idToken JWT string, or null. Used as the HTTP API (/me/*) Authorization header. */
export async function getIdToken() {
  const session = await getSession();
  return session ? session.getIdToken().getJwtToken() : null;
}

/**
 * Current accessToken JWT string, or null. Used as the Bearer token for the AgentCore
 * gateway (/inference/*). The gateway's CUSTOM_JWT authorizer accepts the in-app access
 * token (scope `aws.cognito.signin.user.admin`); the HTTP API instead validates the
 * idToken by `aud`. So: idToken -> HTTP API, accessToken -> gateway. This is also the
 * token a developer pastes into their IDE (OpenAI-compatible api_key).
 */
export async function getAccessToken() {
  const session = await getSession();
  return session ? session.getAccessToken().getJwtToken() : null;
}

/** Decoded claims of the current idToken (email, sub...), or {}. */
export async function getClaims() {
  const session = await getSession();
  if (!session) return {};
  return session.getIdToken().decodePayload() || {};
}
