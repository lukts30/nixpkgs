// SPDX-License-Identifier: GPL-2.0

//! External Rust module exercising allocation and cleanup in User Mode Linux.

use kernel::prelude::*;

module! {
    type: UmlRustTest,
    name: "uml_rust_test",
    license: "GPL",
}

struct UmlRustTest {
    numbers: KVec<i32>,
}

impl kernel::Module for UmlRustTest {
    fn init(_module: &'static ThisModule) -> Result<Self> {
        let mut numbers = KVec::new();
        numbers.push(42, GFP_KERNEL)?;
        numbers.push(108, GFP_KERNEL)?;
        pr_info!("UML Rust module loaded: {:?}\n", numbers);
        Ok(Self { numbers })
    }
}

impl Drop for UmlRustTest {
    fn drop(&mut self) {
        pr_info!("UML Rust module unloaded: {:?}\n", self.numbers);
    }
}
