"""Lightweight mutex from DearCyGui"""

cdef extern from * nogil:
    """
    #include <atomic>
    #include <thread>

    struct DCGMutex {
    private:
        alignas(8) std::atomic<std::thread::id> owner_{std::thread::id()};
        alignas(8) std::atomic<int64_t> count_{0};

    public:
        DCGMutex() noexcept = default;
        
        void lock() noexcept {
            const auto self = std::this_thread::get_id();
            
            while (true) {
                // Try to acquire if unowned
                auto expected = std::thread::id();
                if (owner_.compare_exchange_strong(expected, self, std::memory_order_acquire)) {
                    count_.store(1, std::memory_order_relaxed);
                    return;
                }
                
                // Check if we already own it
                if (expected == self) {
                    count_.fetch_add(1);
                    return;
                }
                
                // Spin wait with sleep
                std::this_thread::sleep_for(std::chrono::microseconds(10));
            }
        }
        
        bool try_lock() noexcept {
            const auto self = std::this_thread::get_id();
            
            auto expected = std::thread::id();
            if (owner_.compare_exchange_strong(expected, self, std::memory_order_acquire)) {
                count_.store(1, std::memory_order_relaxed);
                return true;
            }
            
            if (expected == self) {
                count_.fetch_add(1);
                return true;
            }
            
            return false;
        }
        
        void unlock() noexcept {
            const auto self = std::this_thread::get_id();
            if (owner_.load() != self) {
                return;
            }

            if (count_.fetch_sub(1, std::memory_order_release) == 1) {
                owner_.store(std::thread::id(), std::memory_order_release);
            }
        }

        ~DCGMutex() = default;
        DCGMutex(const DCGMutex&) = delete;
        DCGMutex& operator=(const DCGMutex&) = delete;
    };
    """
    cppclass DCGMutex:
        DCGMutex()
        DCGMutex(DCGMutex&)
        DCGMutex& operator=(DCGMutex&)
        void lock()
        bint try_lock()
        void unlock()

# generated with pxdgen /usr/include/c++/11/mutex -x c++

cdef extern from "<mutex>" namespace "std" nogil:
    cppclass mutex:
        mutex()
        mutex(mutex&)
        mutex& operator=(mutex&)
        void lock()
        bint try_lock()
        void unlock()
    cppclass __condvar:
        __condvar()
        __condvar(__condvar&)
        __condvar& operator=(__condvar&)
        void wait(mutex&)
        #void wait_until(mutex&, timespec&)
        #void wait_until(mutex&, clockid_t, timespec&)
        void notify_one()
        void notify_all()
    cppclass defer_lock_t:
        defer_lock_t()
    cppclass try_to_lock_t:
        try_to_lock_t()
    cppclass adopt_lock_t:
        adopt_lock_t()
    cppclass lock_guard[_Mutex]:
        ctypedef _Mutex mutex_type
        lock_guard(mutex_type&)
        lock_guard(mutex_type&, adopt_lock_t)
        lock_guard(lock_guard&)
        lock_guard& operator=(lock_guard&)
    cppclass scoped_lock[_MutexTypes]:
        #scoped_lock(_MutexTypes &..., ...)
        scoped_lock()
        scoped_lock(_MutexTypes &)
        #scoped_lock(adopt_lock_t, _MutexTypes &...)
        #scoped_lock(scoped_lock&)
        scoped_lock& operator=(scoped_lock&)
    cppclass unique_lock[_Mutex]:
        ctypedef _Mutex mutex_type
        unique_lock()
        unique_lock(mutex_type&)
        unique_lock(mutex_type&, defer_lock_t)
        unique_lock(mutex_type&, try_to_lock_t)
        unique_lock(mutex_type&, adopt_lock_t)
        unique_lock(unique_lock&)
        unique_lock& operator=(unique_lock&)
        #unique_lock(unique_lock&&)
        #unique_lock& operator=(unique_lock&&)
        void lock()
        bint try_lock()
        void unlock()
        void swap(unique_lock&)
        mutex_type* release()
        bint owns_lock()
        mutex_type* mutex()
    void swap[_Mutex](unique_lock[_Mutex]&, unique_lock[_Mutex]&)