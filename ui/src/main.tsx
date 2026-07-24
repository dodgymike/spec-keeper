import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { AuthProvider } from "./auth/AuthContext";
import { AutoRefreshProvider } from "./hooks/autoRefresh";
import "./styles/tokens.css";
import "./styles/base.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("Root element #root not found.");
}

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <AutoRefreshProvider>
          <App />
        </AutoRefreshProvider>
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>
);
