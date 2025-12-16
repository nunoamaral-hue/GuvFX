'use client';

<<<<<<< Updated upstream
import { useState } from 'react';

export default function LoginClient() {
  const [reason, setReason] = useState<string | null>(() => {
    if (typeof window === 'undefined') {
      return null;
    }
    return new URLSearchParams(window.location.search).get('reason');
  });
  void setReason;
=======
import React, { useState } from "react";

export default function LoginClient() {
  const [reason] = useState<string | null>(() => {
    if (typeof window === "undefined") {
      return null;
    }
    return new URLSearchParams(window.location.search).get("reason");
  });
>>>>>>> Stashed changes

  // TODO: put your existing login UI here (or render children passed from page.tsx)
  return (
    <div>
      {reason ? <p style={{ marginBottom: 12 }}>Reason: {reason}</p> : null}
      {/* existing login form/components */}
    </div>
  );
}
