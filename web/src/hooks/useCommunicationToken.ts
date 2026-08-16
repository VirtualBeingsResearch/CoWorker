import { useEffect, useState } from 'react';
import {
  persistCommunicationToken,
  readCommunicationToken,
  subscribeCommunicationToken,
} from '../lib/communicationToken';

export function useCommunicationToken() {
  const [token, setToken] = useState(readCommunicationToken);

  useEffect(() => subscribeCommunicationToken(setToken), []);

  return {
    token,
    setToken: (next: string) => persistCommunicationToken(next.trim()),
  };
}
