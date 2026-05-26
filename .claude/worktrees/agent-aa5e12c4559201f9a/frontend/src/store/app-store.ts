import { create } from "zustand";
import type { IUser, IHistoryEntry, IChatBubble } from "../types/index.ts";

interface AppState {
  // User
  user: IUser;
  setUser: (user: IUser) => void;

  // History (sidebar)
  history: IHistoryEntry[];
  setHistory: (history: IHistoryEntry[]) => void;

  // Active chat session ID
  activeChatId: string;
  setActiveChatId: (id: string) => void;

  // Chat bubbles for current session
  chatBubbles: IChatBubble[];
  addChatBubble: (bubble: IChatBubble) => void;
  clearChatBubbles: () => void;
  replaceLastBubble: (bubble: IChatBubble) => void;

  // Sidebar collapsed state
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (collapsed: boolean) => void;
}

const MOCK_USER: IUser = {
  firstname: "Anonymous",
  lastname: "User",
  email: "anonymous.user@com",
  name: "dummy.user@com",
  displayName: "Dummy User (dummy.user@com)",
};

export const useAppStore = create<AppState>((set) => ({
  user: MOCK_USER,
  setUser: (user) => set({ user }),

  history: [],
  setHistory: (history) => set({ history }),

  activeChatId: "",
  setActiveChatId: (id) => set({ activeChatId: id }),

  chatBubbles: [],
  addChatBubble: (bubble) =>
    set((state) => ({ chatBubbles: [...state.chatBubbles, bubble] })),
  clearChatBubbles: () => set({ chatBubbles: [] }),
  replaceLastBubble: (bubble) =>
    set((state) => {
      const updated = [...state.chatBubbles];
      if (updated.length > 0) updated[updated.length - 1] = bubble;
      return { chatBubbles: updated };
    }),

  sidebarCollapsed: false,
  setSidebarCollapsed: (collapsed) => set({ sidebarCollapsed: collapsed }),
}));
