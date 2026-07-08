use crate::decrypt::WrapperDecryptSession;
use crate::media::{decrypt_and_mux_hex_native, decrypt_and_mux_wrapper_native};
use crate::mux::{
    mux_decrypted_media_direct_native, mux_decrypted_mp4_tracks_native, write_decrypted_m4a_native,
    write_decrypted_mp4_track_native,
};
use pyo3::prelude::*;

#[pyfunction]
fn native_available() -> bool {
    true
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(native_available, module)?)?;
    module.add_function(wrap_pyfunction!(decrypt_and_mux_hex_native, module)?)?;
    module.add_function(wrap_pyfunction!(decrypt_and_mux_wrapper_native, module)?)?;
    module.add_function(wrap_pyfunction!(write_decrypted_m4a_native, module)?)?;
    module.add_function(wrap_pyfunction!(write_decrypted_mp4_track_native, module)?)?;
    module.add_function(wrap_pyfunction!(mux_decrypted_media_direct_native, module)?)?;
    module.add_function(wrap_pyfunction!(mux_decrypted_mp4_tracks_native, module)?)?;
    module.add_class::<WrapperDecryptSession>()?;
    Ok(())
}
