import tensorflow as tf
from src.models.tf_layers import (
    ConvBlock,
    ResidualBlock,
    ConvolutionalPolicyHead,
    ConvolutionalValueOrMovesLeftHead,
)
from src.models.tf_losses import policy_loss, value_loss, moves_left_loss


class LeelaZeroNet(tf.keras.Model):
    def __init__(
        self,
        num_filters,
        num_residual_blocks,
        se_ratio,
        constrain_norms,
        policy_loss_weight,
        value_loss_weight,
        moves_left_loss_weight,
    ):
        super().__init__()
        self.input_reshape = tf.keras.layers.Reshape((112, 8, 8))
        self.input_block = ConvBlock(
            filter_size=3,
            output_channels=num_filters,
            constrain_norms=constrain_norms,
            bn_scale=True,
            name="input_block",
        )
        self.residual_blocks = [
            ResidualBlock(
                channels=num_filters,
                se_ratio=se_ratio,
                constrain_norms=constrain_norms,
                name=f"residual_block_{i}",
            )
            for i in range(num_residual_blocks)
        ]
        self.policy_head = ConvolutionalPolicyHead(
            num_filters=num_filters, constrain_norms=constrain_norms
        )
        # The value head has 3 dimensions for estimating the likelihood of win/draw/loss (WDL)
        self.value_head = ConvolutionalValueOrMovesLeftHead(
            output_dim=3,
            num_filters=32,
            hidden_dim=128,
            constrain_norms=constrain_norms,
            relu=False,
        )
        # Moves left cannot be less than 0, so we use relu to clamp
        self.moves_left_head = ConvolutionalValueOrMovesLeftHead(
            output_dim=1,
            num_filters=8,
            hidden_dim=128,
            constrain_norms=constrain_norms,
            relu=True,
        )
        self.policy_loss_weight = policy_loss_weight
        self.value_loss_weight = value_loss_weight
        self.moves_left_loss_weight = moves_left_loss_weight

    def call(self, inputs, training=None, mask=None):
        flow = self.input_reshape(inputs)
        flow = self.input_block(flow)
        for residual_block in self.residual_blocks:
            flow = residual_block(flow)
        policy_out = self.policy_head(flow)
        value_out = self.value_head(flow)
        moves_left_out = self.moves_left_head(flow)
        return (
            tf.cast(policy_out, tf.float32),
            tf.cast(value_out, tf.float32),
            tf.cast(moves_left_out, tf.float32),
        )

    def train_step(self, inputs):
        input_planes, policy_target, wdl_target, moves_left_target = inputs
        with tf.GradientTape() as tape:
            policy_out, value_out, moves_left_out = self(input_planes)
            p_loss = policy_loss(policy_target, policy_out)
            v_loss = value_loss(wdl_target, value_out)
            ml_loss = moves_left_loss(moves_left_target, moves_left_out)
            total_loss = (
                self.policy_loss_weight * p_loss
                + self.value_loss_weight * v_loss
                + self.moves_left_loss_weight * ml_loss
            )
            if tf.keras.mixed_precision.global_policy().name == "mixed_float16":
                scaled_loss = self.optimizer.get_scaled_loss(total_loss)
                scaled_gradients = tape.gradient(scaled_loss, self.trainable_variables)
                gradients = self.optimizer.get_unscaled_gradients(scaled_gradients)
            else:
                gradients = tape.gradient(total_loss, self.trainable_variables)
        self.optimizer.apply_gradients(zip(gradients, self.trainable_variables))

        return {
            "policy_loss": p_loss,
            "value_loss": v_loss,
            "moves_left_loss": ml_loss,
            "loss": total_loss,
        }
