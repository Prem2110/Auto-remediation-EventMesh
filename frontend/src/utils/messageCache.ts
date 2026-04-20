/**
 * Message Details Cache
 * Stores fetched message details to avoid re-fetching when navigating back
 */

interface CachedMessageDetails {
  data: any;
  timestamp: number;
}

class MessageCache {
  private cache: Map<string, CachedMessageDetails> = new Map();
  private readonly TTL = 5 * 60 * 1000; // 5 minutes cache TTL

  /**
   * Get cached message details
   */
  get(messageId: string): any | null {
    const cached = this.cache.get(messageId);
    
    if (!cached) {
      return null;
    }

    // Check if cache is still valid
    const now = Date.now();
    if (now - cached.timestamp > this.TTL) {
      this.cache.delete(messageId);
      return null;
    }

    return cached.data;
  }

  /**
   * Set message details in cache
   */
  set(messageId: string, data: any): void {
    this.cache.set(messageId, {
      data,
      timestamp: Date.now()
    });
  }

  /**
   * Clear specific message from cache
   */
  clear(messageId: string): void {
    this.cache.delete(messageId);
  }

  /**
   * Clear all cached messages
   */
  clearAll(): void {
    this.cache.clear();
  }

  /**
   * Get cache size
   */
  size(): number {
    return this.cache.size;
  }
}

// Export singleton instance
export const messageCache = new MessageCache();