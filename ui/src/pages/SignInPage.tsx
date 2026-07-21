import { useAuth } from "../auth/AuthContext";
import "./SignInPage.css";

/**
 * Shown when Cognito is configured and there is no active session. Kicks
 * off the Authorization Code + PKCE redirect to the Hosted UI on click.
 */
export function SignInPage() {
  const { signIn, error } = useAuth();

  return (
    <div className="sign-in">
      <div className="sign-in__card">
        <h1 className="sign-in__title">Spec Server</h1>
        <p className="sign-in__subtitle">Sign in to view the dashboard.</p>
        {error ? (
          <p className="sign-in__error" role="alert">
            {error}
          </p>
        ) : null}
        <button type="button" className="sign-in__button" onClick={() => void signIn()}>
          Sign in
        </button>
      </div>
    </div>
  );
}
